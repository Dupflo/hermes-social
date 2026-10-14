"""s23 — fetch_comments_from_camofox, scan options (console ADR 054, D5/P2/P3).

A captcha met during the scan goes to `on_captcha` in the current tab, never after a reload; the tab is closed
when asked; `expired()` bounds the video. With the defaults, the CLI / legacy cron path is unchanged.
Scripted fake client only: no network.
"""

import pytest

from app import camofox_reader
from app.camofox_reader import (
    _COMMENT_EXTRACTION_JS,
    _READER_GUARD_JS,
    GUARD_EVAL_TIMEOUT_S,
    GUARD_PAGE_WAIT_S,
    GUARD_PANEL_WAIT_S,
    GUARD_POLL_S,
    CamofoxHTTPError,
    CamofoxTimeout,
    CamofoxUnreachable,
    fetch_comments_from_camofox,
)
from app.captcha_selectors import PRESENT_JS
from app.comments_panel import CLICK_TIMEOUT_S
from app.tiktok_reply_selectors import PANEL_STATE_JS

VIDEO = "https://www.tiktok.com/@dupflodev/video/7657941429938949399"


def dom(*, comments=(("@alice", "proxy stp", "c1"),), captcha=False, url=VIDEO, logged_in=True):
    return {
        "url": url,
        "title": "Proxy | TikTok",
        "logged_in": logged_in,
        "captcha": captcha,
        "body_preview": "dupflodev · 7-2 Commente PROXY",
        "active_right_panel_preview": "",
        "comment_nodes": [{"author": a, "text": t, "comment_id": c} for a, t, c in comments],
    }


INCOHERENT = dom(url="https://www.tiktok.com/@someone/video/1", comments=())
# s26: the extraction of a page whose structural guard is negative (logged_in = g.state === 'ok' is false).
LOGGED_OUT = dom(comments=(), logged_in=False)

# s26 (console ADR 060, P1): reader guard snapshots.
G_NONE = {"state": "no_signal", "login_gate": False, "items": 0, "panel": False, "icon": False}
G_ICON = {"state": "no_signal", "login_gate": False, "items": 0, "panel": False, "icon": True}
G_OK = {"state": "ok", "login_gate": False, "items": 1, "panel": True, "icon": True}
G_GATE = {"state": "login_gate", "login_gate": True, "items": 0, "panel": False, "icon": False}

# s28 (console ADR 062, D2): panelState() snapshots (invented targets).
TAB = {"selector": "TAB", "x": 1575, "y": 76}
ICON = {"selector": "ICON", "x": 1114, "y": 684}
P_COMMENTS = {"state": "comments", "comments_tab": TAB, "comment_icon": ICON, "comment_count": None}
P_RELATED = {"state": "related", "comments_tab": TAB, "comment_icon": ICON, "comment_count": None}
P_CLOSED = {"state": "closed", "comments_tab": None, "comment_icon": ICON, "comment_count": None}
# One activation = its first panelState() (logged "activate", R3), then clicks / re-snapshot of the same activation.
ACTIVATION_NAMES = ("activate", "panel_state", "click_selector", "mouse_available", "mouse")


class FakeClient:
    """Scripted Camofox client.

    s26: a fake monotonic clock (`clock()`), advanced by the injected `sleep` and by a scriptable cost per call
    (`costs`, by log name; `poll_cost_s` for the guard polls). `guards` scripts the reader guard snapshots: a list
    (last one repeated), a dict {"page": [...], "panel": [...]} (per kind, last one repeated) or a callable
    (kind, extracts_so_far) -> snapshot. A guard poll is logged ("guard", kind), kind = "panel" right after an
    activation, "page" otherwise. `guard_raises` maps a poll index (0-based, all kinds) to the exception it raises.

    s28 (console ADR 062): `panels` scripts the panelState() snapshots (last one repeated). The first panelState() of
    an activation is logged ("activate",), a later one of the same activation ("panel_state",) (R3). Trusted clicks
    are logged ("click_selector", selector) / ("mouse", x, y); `click_errors` scripts the click_selector errors
    (None = success), `mouse_ok` the plugin probe.
    """

    def __init__(self, *, present=(), doms=(dom(),), create_error=None, delete_error=None, guards=(G_OK,),
                 guard_raises=None, panels=(P_COMMENTS,), click_errors=None, mouse_ok=False, poll_cost_s=0.0,
                 costs=None):
        self.log = []
        self.stamps = []
        self.present = list(present)
        self.doms = list(doms)
        self.create_error = create_error
        self.delete_error = delete_error
        self.guards = guards if callable(guards) or isinstance(guards, dict) else list(guards)
        self.kind_guards = {k: list(v) for k, v in guards.items()} if isinstance(guards, dict) else None
        self.guard_raises = dict(guard_raises or {})
        self.panels = list(panels)
        self.click_errors = list(click_errors or [])
        self.mouse_ok = mouse_ok
        self.panel_timeouts = []
        self.costs = dict(costs or {})
        if poll_cost_s:
            self.costs["guard"] = poll_cost_s
        self.now = 0.0
        self.polls = 0
        self.guard_timeouts = []
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def _record(self, *entry):
        self.log.append(entry)
        self.stamps.append(self.now)
        self.now += self.costs.get(entry[0], 0.0)

    def create_tab(self, *, session_key, url):
        self._record("create_tab", session_key)
        if self.create_error is not None:
            raise self.create_error
        return "tab-1"

    def _guard(self, kind):
        if callable(self.guards):
            return self.guards(kind, self.names().count("extract"))
        if self.kind_guards is not None:
            seq = self.kind_guards[kind]
            return seq.pop(0) if len(seq) > 1 else seq[0]
        return self.guards.pop(0) if len(self.guards) > 1 else self.guards[0]

    def evaluate(self, *, tab_id, expression, timeout=None):
        assert tab_id == "tab-1"
        if expression == _READER_GUARD_JS:
            previous = [name for name in self.names() if name != "guard"]
            kind = "panel" if previous and previous[-1] in ACTIVATION_NAMES else "page"
            index = self.polls
            self.polls += 1
            self.guard_timeouts.append(timeout)
            self._record("guard", kind)
            if index in self.guard_raises:
                raise self.guard_raises[index]
            return dict(self._guard(kind))
        if expression == PRESENT_JS:
            self._record("present")
            return self.present.pop(0) if self.present else False
        if expression == PANEL_STATE_JS:
            first = not self.log or self.log[-1][0] not in ACTIVATION_NAMES
            self.panel_timeouts.append(timeout)
            self._record("activate" if first else "panel_state")
            return dict(self.panels.pop(0) if len(self.panels) > 1 else self.panels[0])
        if expression == _COMMENT_EXTRACTION_JS:
            self._record("extract")
            return self.doms.pop(0) if len(self.doms) > 1 else self.doms[0]
        if expression.startswith("location.replace("):
            self._record("reload", expression)
            return True
        raise AssertionError("unexpected expression")

    def click_selector(self, tab_id, selector, *, timeout=None):
        assert tab_id == "tab-1" and timeout == CLICK_TIMEOUT_S
        self._record("click_selector", selector)
        if self.click_errors:
            error = self.click_errors.pop(0)
            if error is not None:
                raise error

    def mouse_available(self, *, timeout=5.0):
        self._record("mouse_available")
        return self.mouse_ok

    def mouse(self, tab_id, actions, *, hold=False, timeout=25.0):
        assert tab_id == "tab-1" and timeout == CLICK_TIMEOUT_S
        self._record("mouse", actions[0]["x"], actions[0]["y"])
        return len(actions)

    def delete_tab(self, tab_id, *, timeout=None):
        self._record("delete_tab", tab_id, timeout)
        if self.delete_error is not None:
            raise self.delete_error

    def names(self, skip=()):
        return [entry[0] for entry in self.log if entry[0] not in skip]

    def count(self, name, kind=None):
        return sum(1 for entry in self.log if entry[0] == name and (kind is None or entry[1] == kind))

    def stamp_of(self, name, kind=None, nth=0):
        hits = [t for entry, t in zip(self.log, self.stamps) if entry[0] == name and (kind is None or entry[1] == kind)]
        return hits[nth]


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
    kwargs.setdefault("sleep", client.sleep)
    kwargs.setdefault("clock", client.clock)
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
    # s26 : épingle amendée (sondages de garde, console ADR 060)
    names = fake.names(skip=("guard",))
    assert names[:4] == ["create_tab", "present", "on_captcha", "activate"]
    assert "reload" not in names
    assert names[-1] == "delete_tab"
    assert capsys.readouterr() == ("", "")


def test_captcha_before_a_reload_is_handed_over_first_and_skips_that_reload():
    fake = FakeClient(present=[False, True, False], doms=(INCOHERENT, dom()))
    solver = Solver(fake, [True])

    result = scan(fake, on_captcha=solver)

    assert result["ok"] is True
    # s26 : épingle amendée (sondages de garde, console ADR 060) — 1 sondage page + 1 panneau par tour
    assert fake.count("guard", "page") == 2 and fake.count("guard", "panel") == 2
    names = fake.names(skip=("guard",))
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

    # s26 : épingle amendée (sondages de garde, console ADR 060) — `expired` vrai dès qu'une extraction a eu lieu
    def expired():
        return fake.names().count("extract") >= 1

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


# --- s26 (console ADR 060, D2/D3/D4, P2/P3/P4): active bounded wait, one retry without reload, guard sub-code ---


def by_extracts(*per_round):
    """Guard snapshot by number of extractions so far (round 0, 1, …; last one repeated)."""
    return lambda kind, extracts: per_round[min(extracts, len(per_round) - 1)]


def only_poll_sleeps(fake):
    return all(seconds == GUARD_POLL_S for seconds in fake.sleeps)


def test_partially_loaded_then_logged_in_polls_until_ready_without_fixed_pause():
    fake = FakeClient(guards=[G_NONE, G_NONE, G_NONE, G_ICON, G_OK])

    result = scan(fake, on_captcha=Solver(fake, []))

    assert result["ok"] is True
    assert fake.count("guard", "page") == 4
    assert fake.count("guard", "panel") == 1
    assert only_poll_sleeps(fake)  # no pause(8), no pause(2)
    assert result["guard"] == "ok"


def test_complete_logged_in_page_is_one_page_poll_and_one_panel_poll():
    fake = FakeClient()

    result = scan(fake, on_captcha=Solver(fake, []))

    assert result["ok"] is True
    assert fake.count("guard", "page") == 1 and fake.count("guard", "panel") == 1
    assert result["guard"] == "ok"


def test_every_poll_has_the_eval_timeout():
    fake = FakeClient(guards=[G_NONE, G_ICON, G_OK])

    scan(fake, on_captcha=Solver(fake, []))

    assert fake.guard_timeouts and all(t == GUARD_EVAL_TIMEOUT_S for t in fake.guard_timeouts)


def test_transient_false_negative_is_retried_once_in_the_same_tab():
    fake = FakeClient(guards=by_extracts(G_NONE, G_OK), doms=(LOGGED_OUT, dom()), poll_cost_s=0.5)

    result = scan(fake, on_captcha=Solver(fake, []))

    assert result["ok"] is True
    assert result["guard"] == "ok_retry"
    assert fake.count("present") == 2
    assert fake.count("reload") == 0
    assert fake.count("extract") == 2
    first_wait = fake.stamp_of("activate") - fake.stamp_of("guard", "page")
    assert GUARD_PAGE_WAIT_S <= first_wait < GUARD_PAGE_WAIT_S + GUARD_POLL_S + 0.5
    assert fake.names()[-1] == "delete_tab"


def test_late_captcha_at_the_head_of_the_retry_goes_to_the_solver():
    fake = FakeClient(present=[False, True], guards=by_extracts(G_NONE, G_OK), doms=(LOGGED_OUT, dom()))
    solver = Solver(fake, [True])

    result = scan(fake, on_captcha=solver)

    assert result["ok"] is True
    assert result["guard"] == "captcha_late"
    assert fake.count("reload") == 0
    assert "on_captcha" in fake.names()


def test_late_captcha_at_the_head_of_the_retry_unsolved_closes_the_tab():
    fake = FakeClient(present=[False, True], guards=by_extracts(G_NONE, G_OK), doms=(LOGGED_OUT, dom()))

    result = scan(fake, on_captcha=Solver(fake, [False]))

    assert result["ok"] is False and result["error"] == "captcha_unsolved"
    assert result["guard"] == "captcha_late"
    assert fake.log[-1] == ("delete_tab", "tab-1", 10)


def test_late_captcha_after_the_second_wait_is_probed_before_the_verdict():
    fake = FakeClient(
        present=[False, False, True], guards=by_extracts(G_NONE, G_NONE, G_OK), doms=(LOGGED_OUT, LOGGED_OUT, dom())
    )

    result = scan(fake, on_captcha=Solver(fake, [True]))

    assert result["ok"] is True
    assert result["guard"] == "captcha_late"
    assert fake.count("present") == 3
    assert fake.count("extract") == 3
    assert fake.count("reload") == 0


def test_late_captcha_after_the_second_wait_unsolved_is_captcha_unsolved():
    fake = FakeClient(present=[False, False, True], guards=by_extracts(G_NONE), doms=(LOGGED_OUT,))

    result = scan(fake, on_captcha=Solver(fake, [False]))

    assert result["ok"] is False and result["error"] == "captcha_unsolved"
    assert result["guard"] == "captcha_late"
    assert fake.log[-1] == ("delete_tab", "tab-1", 10)


def test_real_logged_out_page_waits_every_budget_then_is_not_actionable():
    fake = FakeClient(guards=[G_GATE], doms=(LOGGED_OUT,))

    result = scan(fake, on_captcha=Solver(fake, []))

    assert result["ok"] is False and result["error"] == "tiktok_page_not_actionable"
    assert result["guard"] == "login_gate"
    assert fake.count("present") == 3  # head, retry head, final probe
    assert fake.count("reload") == 0
    assert fake.count("extract") == 2
    for nth in (0, 1):  # login_gate is never decisive early: each page wait lasts its whole budget
        start = fake.stamp_of("guard", "page", nth=nth)
        assert fake.stamp_of("activate", nth=nth) - start >= GUARD_PAGE_WAIT_S
    assert fake.log[-1] == ("delete_tab", "tab-1", 10)


def test_no_signal_at_all_is_not_actionable_with_no_signal():
    fake = FakeClient(guards=[G_NONE], doms=(LOGGED_OUT,))

    result = scan(fake, on_captcha=Solver(fake, []))

    assert result["error"] == "tiktok_page_not_actionable"
    assert result["guard"] == "no_signal"


def test_comment_icon_alone_everywhere_is_not_actionable_with_no_signal():
    fake = FakeClient(guards=[G_ICON], doms=(LOGGED_OUT,))

    result = scan(fake, on_captcha=Solver(fake, []))

    assert result["error"] == "tiktok_page_not_actionable"
    assert result["guard"] == "no_signal"
    assert fake.count("guard", "page") == 2  # the icon makes the page ready for the activation, nothing more


def test_login_gate_on_early_panel_polls_then_ok_is_ok():
    fake = FakeClient(guards={"page": [G_OK], "panel": [G_GATE, G_GATE, G_OK]})

    result = scan(fake, on_captcha=Solver(fake, []))

    assert result["ok"] is True
    assert fake.count("guard", "panel") == 3
    assert result["guard"] == "ok"


def test_negative_guard_but_extracted_comments_is_ok():
    fake = FakeClient(guards=[G_NONE], doms=(dom(logged_in=False),))

    result = scan(fake, on_captcha=Solver(fake, []))

    assert result["ok"] is True
    assert result["comments"] == [{"id": "c1", "author": "@alice", "text": "proxy stp"}]
    assert fake.count("extract") == 1
    assert result["guard"] == "ok"


# s28 : épingle amendée (D5, R6, console ADR 062) — la réactivation à mi-budget est désormais inconditionnelle.
def test_reactivation_at_mid_budget_when_the_first_activation_clicked_nothing():
    panel = [G_ICON] * 8 + [G_OK]
    fake = FakeClient(guards={"page": [G_ICON], "panel": panel}, panels=[{"state": "unknown"}])

    result = scan(fake, on_captcha=Solver(fake, []))

    assert result["ok"] is True
    assert fake.count("activate") == 2
    second = fake.stamp_of("activate", nth=1) - fake.stamp_of("activate", nth=0)
    assert second >= GUARD_PANEL_WAIT_S / 2


# s28 : épingle inversée (D5, R6, console ADR 062) — ex test_no_reactivation_when_the_first_activation_clicked.
def test_reactivation_at_mid_budget_even_when_the_first_activation_clicked():
    fake = FakeClient(guards={"page": [G_ICON], "panel": [G_ICON] * 8 + [G_OK]}, panels=[P_RELATED])

    result = scan(fake, on_captcha=Solver(fake, []))

    assert result["ok"] is True
    assert fake.count("activate") == 2
    assert fake.count("click_selector") == 2
    attempts = result["diagnostics"]["activation"]["attempts"]
    assert [a["clicked"] for a in attempts] == ["comments_tab", "comments_tab"]


def test_expired_during_a_wait_is_a_video_timeout_without_further_evaluate():
    fake = FakeClient(guards=[G_NONE])

    result = scan(fake, on_captcha=Solver(fake, []), expired=lambda: fake.polls >= 3)

    assert result["ok"] is False and result["error"] == "video_timeout"
    assert fake.names() == ["create_tab", "present", "guard", "guard", "guard", "delete_tab"]


def test_a_timed_out_poll_counts_no_signal_and_the_wait_goes_on():
    fake = FakeClient(guards=[G_NONE, G_OK], guard_raises={1: CamofoxTimeout()})

    result = scan(fake, on_captcha=Solver(fake, []))

    assert result["ok"] is True
    assert fake.count("guard", "page") == 3
    assert result["guard"] == "ok"


def test_another_unreachable_error_on_a_poll_is_camofox_unreachable():
    fake = FakeClient(guards=[G_NONE], guard_raises={1: CamofoxUnreachable()})

    result = scan(fake, on_captcha=Solver(fake, []))

    assert result["ok"] is False and result["error"] == "camofox_unreachable"
    assert fake.log[-1] == ("delete_tab", "tab-1", 10)


def test_early_error_before_any_poll_has_no_guard_key():
    fake = FakeClient(present=[True])

    result = scan(fake, on_captcha=Solver(fake, [False]))

    assert "guard" not in result
    assert fake.polls == 0


def test_unknown_guard_state_is_no_signal():
    fake = FakeClient(guards=[{"state": "weird", "items": 9, "panel": True, "icon": True}], doms=(LOGGED_OUT,))

    result = scan(fake, on_captcha=Solver(fake, []))

    assert result["error"] == "tiktok_page_not_actionable"
    assert result["guard"] == "no_signal"


def test_cli_mode_transient_false_negative_retries_without_any_probe():
    fake = FakeClient(guards=by_extracts(G_NONE, G_OK), doms=(LOGGED_OUT, dom()))

    result = fetch_comments_from_camofox(video_url=VIDEO, client=fake, sleep=fake.sleep, clock=fake.clock)

    assert result["ok"] is True
    assert result["guard"] == "ok_retry"
    assert "present" not in fake.names()
    assert "delete_tab" not in fake.names()
    assert fake.count("extract") == 2 and fake.count("reload") == 0


# --- s26 (console ADR 060, D2/P5): one video stays bounded once `expired()` turns true ---

# s28 : épingle amendée (T4, console ADR 062) — l'activation est un panelState() borné à 10 s (« activate » pour le
# premier d'une activation, « panel_state » pour celui d'après l'icône), des clics `click_selector` et `mouse` bornés à
# CLICK_TIMEOUT_S et la sonde `mouse_available` (5 s), chacun précédé d'un contrôle d'`expired()`.
MAX_COSTS = {"create_tab": 30.0, "activate": GUARD_EVAL_TIMEOUT_S, "panel_state": GUARD_EVAL_TIMEOUT_S,
             "click_selector": CLICK_TIMEOUT_S, "mouse": CLICK_TIMEOUT_S, "mouse_available": 5.0, "extract": 30.0,
             "reload": 30.0, "present": 30.0, "guard": GUARD_EVAL_TIMEOUT_S}
IN_FLIGHT_S = 92.0  # ADR 054 §5: one try in flight (30 + 2 + 30 + 30)
PROBE_S = 30.0  # PRESENT_JS probe, counted apart as before
SCAN_TRY_BOUND_S = 170.0  # T4: 220 - (list_tabs 10 + PRESENT_JS probe 30 + delete_tab 10), tiktok_scan.py


def _bound_fake(scenario):
    if scenario == "connected":
        return FakeClient(costs=MAX_COSTS)
    if scenario == "logged_out":
        return FakeClient(costs=MAX_COSTS, guards=[G_GATE], doms=(LOGGED_OUT,))
    if scenario == "drift":
        return FakeClient(costs=MAX_COSTS, doms=(INCOHERENT,))
    # s28: closed panel each time, every click_selector in error (422), mouse fallback, never readable.
    return FakeClient(costs=MAX_COSTS, guards=[G_ICON], doms=(LOGGED_OUT,), panels=[P_CLOSED, P_RELATED, P_CLOSED],
                      click_errors=[CamofoxHTTPError(422)] * 50, mouse_ok=True)


def _worst_in_flight(scenario):
    worst = 0.0
    for switch in range(0, 301):
        fake = _bound_fake(scenario)
        scan(fake, on_captcha=Solver(fake, []), expired=lambda: fake.now >= switch, close_tab=False)
        if fake.now > switch:
            worst = max(worst, fake.now - switch)
    return worst


@pytest.mark.parametrize("scenario", ["connected", "logged_out", "drift", "clicks_fallback"])
def test_one_video_returns_within_one_try_in_flight_after_expiry(scenario):
    worst = _worst_in_flight(scenario)
    assert worst <= IN_FLIGHT_S + PROBE_S, (scenario, worst)
    assert IN_FLIGHT_S + PROBE_S <= SCAN_TRY_BOUND_S


def test_the_clicks_scenario_really_clicks_and_falls_back():
    fake = _bound_fake("clicks_fallback")
    scan(fake, on_captcha=Solver(fake, []), close_tab=False)
    assert fake.count("click_selector") >= 2 and fake.count("mouse") >= 2
    assert fake.count("panel_state") >= 1


# --- s26 (console ADR 060, D5): AC2, a drifted tab is re-navigated to the target or dropped cleanly ---


def test_drift_then_renavigation_reads_the_target_and_closes_the_tab():
    fake = FakeClient(doms=(INCOHERENT, dom()))

    result = scan(fake, on_captcha=Solver(fake, []))

    assert result["ok"] is True
    reloads = [entry for entry in fake.log if entry[0] == "reload"]
    assert len(reloads) == 1
    assert reloads[0][1].startswith("location.replace(") and "_hermes_reload=1_" in reloads[0][1]
    assert fake.names()[-1] == "delete_tab"


def test_persistent_drift_is_target_video_incoherent_and_closes_the_tab():
    fake = FakeClient(doms=(INCOHERENT,))

    result = scan(fake, on_captcha=Solver(fake, []))

    assert result["ok"] is False and result["error"] == "target_video_incoherent"
    assert result["detail"] == "loaded_different_video"
    assert fake.count("reload") == 2
    assert fake.log[-1] == ("delete_tab", "tab-1", 10)


def test_persistent_drift_in_cli_mode_is_incoherent_without_closing():
    fake = FakeClient(doms=(INCOHERENT,))

    result = fetch_comments_from_camofox(video_url=VIDEO, client=fake, sleep=fake.sleep, clock=fake.clock)

    assert result["ok"] is False and result["error"] == "target_video_incoherent"
    assert fake.count("reload") == 2
    assert "delete_tab" not in fake.names()


# --- s28 (console ADR 062, D3-D6): trusted clicks to open the comments panel, second activation before giving up ---


def test_panel_wait_is_twelve_seconds():
    assert GUARD_PANEL_WAIT_S == 12.0


def test_you_may_like_then_readable_clicks_the_comments_tab_and_reads():
    fake = FakeClient(guards={"page": [G_ICON], "panel": [G_ICON, G_OK]}, panels=[P_RELATED])

    result = scan(fake, on_captcha=Solver(fake, []))

    assert result["ok"] is True
    assert result["comments"] == [{"id": "c1", "author": "@alice", "text": "proxy stp"}]
    assert [e for e in fake.log if e[0] == "click_selector"] == [("click_selector", "TAB")]
    assert fake.count("activate") == 1
    assert fake.panel_timeouts and all(t == GUARD_EVAL_TIMEOUT_S for t in fake.panel_timeouts)
    assert result["diagnostics"]["activation"] == {
        "method": "real_click",
        "attempts": [{"state": "related", "clicked": "comments_tab", "via": "selector", "error": None}],
        "comment_count": None,
    }


def test_closed_panel_clicks_the_icon_then_the_tab():
    fake = FakeClient(guards={"page": [G_ICON], "panel": [G_OK]}, panels=[P_CLOSED, P_RELATED])

    result = scan(fake, on_captcha=Solver(fake, []))

    assert result["ok"] is True
    assert [e[1] for e in fake.log if e[0] == "click_selector"] == ["ICON", "TAB"]
    assert fake.names(skip=("guard",))[:6] == [
        "create_tab", "present", "activate", "click_selector", "panel_state", "click_selector",
    ]
    assert result["diagnostics"]["activation"]["attempts"][0]["clicked"] == "comment_icon+comments_tab"


def test_ambiguous_click_falls_back_to_the_mouse():
    fake = FakeClient(panels=[P_RELATED], click_errors=[CamofoxHTTPError(422)], mouse_ok=True)

    result = scan(fake, on_captcha=Solver(fake, []))

    assert result["ok"] is True
    assert ("mouse", 1575.0, 76.0) in fake.log
    assert result["diagnostics"]["activation"]["attempts"][0]["via"] == "mouse"


def test_an_activation_without_effect_gets_a_second_one_before_not_actionable():
    fake = FakeClient(guards={"page": [G_ICON], "panel": [G_ICON]}, panels=[P_RELATED], doms=(LOGGED_OUT,))

    result = scan(fake, on_captcha=Solver(fake, []))

    assert result["ok"] is False and result["error"] == "tiktok_page_not_actionable"
    assert fake.count("extract") == 2  # the first round and the retry (no reload)
    assert fake.count("activate") == 4  # exactly 2 activations per reading round
    for nth in (0, 2):
        second = fake.stamp_of("activate", nth=nth + 1) - fake.stamp_of("activate", nth=nth)
        assert second >= GUARD_PANEL_WAIT_S / 2
    assert len(result["diagnostics"]["activation"]["attempts"]) == 2


def test_video_without_comment_is_ok_with_zero_comment():
    empty_panel = {"state": "ok", "login_gate": False, "items": 0, "panel": True, "icon": True}
    fake = FakeClient(
        guards={"page": [G_ICON], "panel": [empty_panel]},
        panels=[dict(P_RELATED, comment_count=0)],
        doms=(dom(comments=()),),
    )

    result = scan(fake, on_captcha=Solver(fake, []))

    assert result["ok"] is True and result["comments"] == []
    assert result["diagnostics"]["expected_comment_count"] == 0


def test_already_open_panel_is_not_clicked_and_reads():
    fake = FakeClient(panels=[P_COMMENTS])

    result = scan(fake, on_captcha=Solver(fake, []))

    assert result["ok"] is True
    assert fake.count("click_selector") == 0 and fake.count("mouse") == 0
    assert result["diagnostics"]["activation"]["attempts"] == [
        {"state": "comments", "clicked": None, "via": None, "error": None}
    ]


def test_the_count_of_the_snapshot_feeds_comments_expected_but_not_extracted():
    fake = FakeClient(panels=[dict(P_COMMENTS, comment_count=3)], doms=(dom(comments=()),))

    result = scan(fake, on_captcha=Solver(fake, []))

    assert result["ok"] is False and result["error"] == "comments_expected_but_not_extracted"
    assert result["diagnostics"]["expected_comment_count"] == 3


def test_the_activation_diagnostic_carries_no_url_nor_handle():
    fake = FakeClient(panels=[P_CLOSED, P_RELATED], click_errors=[None, CamofoxHTTPError(422)])

    result = scan(fake, on_captcha=Solver(fake, []))

    activation = result["diagnostics"]["activation"]
    assert set(activation) == {"method", "attempts", "comment_count"}
    assert "http" not in repr(activation).replace("http_422", "") and "@" not in repr(activation)


def test_the_reader_has_no_synthetic_click_left():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "camofox_reader.py").read_text(encoding="utf-8")
    panel = (Path(__file__).resolve().parents[1] / "app" / "comments_panel.py").read_text(encoding="utf-8")
    for text in (source, panel, PANEL_STATE_JS):
        for forbidden in ("dispatchEvent", "el.click()", ".click();", "scrollIntoView", "MouseEvent"):
            assert forbidden not in text, forbidden
    assert not hasattr(camofox_reader, "_ACTIVATE_COMMENTS_TAB_JS")


# --- s29 (console ADR 063, D1/D2): the reason of an incoherent result and the author check of an ok one ---


def authored(*handles, **kwargs):
    """dom() whose bounded author area gives `handles` (other handles are invented)."""
    return {**dom(**kwargs), "video_author_handles": list(handles)}


def test_persistent_author_link_of_another_account_is_incoherent_with_its_reason():
    fake = FakeClient(doms=(authored("other.one"),))

    result = scan(fake, on_captcha=Solver(fake, []))

    assert result["ok"] is False and result["error"] == "target_video_incoherent"
    assert result["detail"] == result["incoherent_reason"] == "author_link_mismatch"
    assert "author_check" not in result
    assert fake.count("reload") == 2
    assert fake.log[-1] == ("delete_tab", "tab-1", 10)


def test_author_mismatch_then_the_target_after_a_reload_is_ok_with_match():
    fake = FakeClient(doms=(authored("other.one"), authored("dupflodev")))

    result = scan(fake, on_captcha=Solver(fake, []))

    assert result["ok"] is True and result["author_check"] == "match"
    assert "incoherent_reason" not in result
    assert fake.count("reload") == 1


def test_logged_in_on_a_page_without_the_target_id_is_video_id_missing():
    fake = FakeClient(doms=(dom(url="https://www.tiktok.com/foryou"),))

    result = scan(fake, on_captcha=Solver(fake, []))

    assert result["ok"] is False and result["error"] == "target_video_incoherent"
    assert result["incoherent_reason"] == "video_id_missing"
    assert fake.count("reload") == 2


def test_historical_dom_without_author_area_is_ok_with_id_only():
    fake = FakeClient()

    result = scan(fake, on_captcha=Solver(fake, []))

    assert result["ok"] is True and result["author_check"] == "id_only"
    assert fake.count("reload") == 0


def test_historical_drift_carries_loaded_different_video_as_reason():
    fake = FakeClient(doms=(INCOHERENT,))

    result = scan(fake, on_captcha=Solver(fake, []))

    assert result["incoherent_reason"] == "loaded_different_video"
