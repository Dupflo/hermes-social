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
    _READER_GUARD_JS,
    GUARD_EVAL_TIMEOUT_S,
    GUARD_PAGE_WAIT_S,
    GUARD_PANEL_WAIT_S,
    GUARD_POLL_S,
    CamofoxTimeout,
    CamofoxUnreachable,
    fetch_comments_from_camofox,
)
from app.captcha_selectors import PRESENT_JS

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


class FakeClient:
    """Scripted Camofox client.

    s26: a fake monotonic clock (`clock()`), advanced by the injected `sleep` and by a scriptable cost per call
    (`costs`, by log name; `poll_cost_s` for the guard polls). `guards` scripts the reader guard snapshots: a list
    (last one repeated), a dict {"page": [...], "panel": [...]} (per kind, last one repeated) or a callable
    (kind, extracts_so_far) -> snapshot. A guard poll is logged ("guard", kind), kind = "panel" right after an
    activation, "page" otherwise. `guard_raises` maps a poll index (0-based, all kinds) to the exception it raises.
    """

    def __init__(self, *, present=(), doms=(dom(),), create_error=None, delete_error=None, guards=(G_OK,),
                 guard_raises=None, activations=None, poll_cost_s=0.0, costs=None):
        self.log = []
        self.stamps = []
        self.present = list(present)
        self.doms = list(doms)
        self.create_error = create_error
        self.delete_error = delete_error
        self.guards = guards if callable(guards) or isinstance(guards, dict) else list(guards)
        self.kind_guards = {k: list(v) for k, v in guards.items()} if isinstance(guards, dict) else None
        self.guard_raises = dict(guard_raises or {})
        self.activations = list(activations or [{"clicked": True, "comment_icon_candidates": []}])
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
            kind = "panel" if previous and previous[-1] == "activate" else "page"
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
        if expression == _ACTIVATE_COMMENTS_TAB_JS:
            self._record("activate")
            return dict(self.activations.pop(0) if len(self.activations) > 1 else self.activations[0])
        if expression == _COMMENT_EXTRACTION_JS:
            self._record("extract")
            return self.doms.pop(0) if len(self.doms) > 1 else self.doms[0]
        if expression.startswith("location.replace("):
            self._record("reload", expression)
            return True
        raise AssertionError("unexpected expression")

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


def test_reactivation_at_mid_budget_only_when_the_first_activation_clicked_nothing():
    panel = [G_ICON] * 6 + [G_OK]
    fake = FakeClient(guards={"page": [G_ICON], "panel": panel}, activations=[{"clicked": False}, {"clicked": True}])

    result = scan(fake, on_captcha=Solver(fake, []))

    assert result["ok"] is True
    assert fake.count("activate") == 2
    second = fake.stamp_of("activate", nth=1) - fake.stamp_of("activate", nth=0)
    assert second >= GUARD_PANEL_WAIT_S / 2


def test_no_reactivation_when_the_first_activation_clicked():
    fake = FakeClient(guards={"page": [G_ICON], "panel": [G_ICON] * 6 + [G_OK]}, activations=[{"clicked": True}])

    result = scan(fake, on_captcha=Solver(fake, []))

    assert result["ok"] is True
    assert fake.count("activate") == 1


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

MAX_COSTS = {"create_tab": 30.0, "activate": 30.0, "extract": 30.0, "reload": 30.0, "present": 30.0,
             "guard": GUARD_EVAL_TIMEOUT_S}
IN_FLIGHT_S = 92.0  # ADR 054 §5: one try in flight (30 + 2 + 30 + 30)
PROBE_S = 30.0  # PRESENT_JS probe, counted apart as before


@pytest.mark.parametrize("scenario", ["connected", "logged_out", "drift"])
def test_one_video_returns_within_one_try_in_flight_after_expiry(scenario):
    worst = 0.0
    for switch in range(0, 301):
        if scenario == "connected":
            fake = FakeClient(costs=MAX_COSTS)
        elif scenario == "logged_out":
            fake = FakeClient(costs=MAX_COSTS, guards=[G_GATE], doms=(LOGGED_OUT,))
        else:
            fake = FakeClient(costs=MAX_COSTS, doms=(INCOHERENT,))

        scan(fake, on_captcha=Solver(fake, []), expired=lambda: fake.now >= switch, close_tab=False)

        if fake.now > switch:
            worst = max(worst, fake.now - switch)
    assert worst <= IN_FLIGHT_S + PROBE_S, (scenario, worst)


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
