"""s25 — the run 631 sequence (rotate v2, exit_18), replayed by a simulated Camofox (console ADR 059).

The fixture holds the observed chronology (no content data) and the simulation parameters: humanised moves
of 1.5 s, the plugin ceiling of 20 s per request, a captcha button detached for 3 s after a failure, an envelope
without variant for 8 s after the button is clicked. The simulator first reproduces the three observed tries
with the pre-s25 behaviour, then the real solve_captcha is run on the same scenario.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from app import captcha_geometry as geo
from app import captcha_selectors as sel
from app import captcha_solver as solver
from app.camofox_reader import CamofoxHTTPError
from app.captcha_solver import CaptchaReport, solve_captcha
from test_captcha_geometry import _pre_s25_drag
from test_captcha_solver import BOX_BAR, BOX_HANDLE, KEY, Clock, FakeCamofox, FakeSad, facts_for

FIXTURE = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "captcha" / "run631_rotate_v2.json").read_text(encoding="utf-8")
)
SIM = FIXTURE["simulation"]


class Run631Camofox(FakeCamofox):
    """Camofox as seen during run 631: humanised moves, 20 s ceiling, a captcha button and an envelope in motion."""

    def __init__(self, clock: Clock, *, move_cost_s: float = SIM["move_cost_s"]) -> None:
        super().__init__("rotate_v2", clock, verdicts=SIM["verdicts"])
        self.move_cost_s = move_cost_s
        self.plugin_max_s = float(SIM["plugin_max_s"])
        self.blank_until = float("-inf")
        self.blank = dict(facts_for(None), present=True)  # envelope shown, no variant identifier yet
        self.facts_at = lambda t: self.blank if t < self.blank_until else self.facts

    def evaluate_value(self, tab_id, expression, *, timeout=None):
        value = super().evaluate_value(tab_id, expression, timeout=timeout)
        if expression == sel.PRESENT_JS and value is True:
            # the captcha is still there after a gesture: its button is redrawn, detached for a few seconds
            self.refresh_visible_at = self.clock.t + SIM["refresh_detached_s_after_failure"]
        return value

    def mouse(self, tab_id, actions, *, hold=False, timeout=25.0):
        try:
            return super().mouse(tab_id, actions, hold=hold, timeout=timeout)
        except CamofoxHTTPError:
            # the plugin answered 504 after releasing: the try failed, the captcha's button is redrawn
            self.refresh_visible_at = self.clock.t + SIM["refresh_detached_s_after_failure"]
            raise

    def click_selector(self, tab_id, selector, *, timeout=None):
        self.calls.append(("click", selector))
        if selector == sel.REFRESH_BUTTON and self.clock.t < self.refresh_visible_at:
            raise CamofoxHTTPError(500)  # "Element not actionable … likely detached after page change"
        self.refresh_clicked()

    def refresh_clicked(self):
        self.blank_until = self.clock.t + SIM["blank_envelope_s_after_refresh"]


def _sad():
    return FakeSad(answers={"rotate": SIM["sadcaptcha_angle"]})


def test_fixture_holds_the_observed_sequence():
    observed = FIXTURE["observed"]
    assert [(o["try"], o["result"]) for o in observed] == [(1, "failed"), (2, "failed"), (3, "unrecognized")]
    assert observed[0]["mouse"] == {"status": 504, "ms": 20350}
    assert observed[1]["label"].startswith("essai qui suit un rafraîchissement raté")
    text = json.dumps(FIXTURE).lower()
    for banned in ("http", "@", "sadkey", "base64", "licensekey"):
        assert banned not in text.replace("hermes-console", "")


# --- reproduction: the simulator replays the three observed tries with the pre-s25 behaviour ------------------


def test_simulator_reproduces_run_631():
    clock = Clock()
    client = Run631Camofox(clock)
    # try 1: the pre-s25 rotate gesture, in ONE request ⇒ 504 after the 20 s ceiling
    distance = geo.rotate_distance(BOX_BAR["width"], BOX_HANDLE["width"], SIM["sadcaptcha_angle"])
    start = geo.drag_start(BOX_HANDLE)
    began = clock()
    with pytest.raises(CamofoxHTTPError) as info:
        client.mouse("tab-1", _pre_s25_drag(start[0], start[1], distance), hold=False)
    assert info.value.status == 504
    assert clock() - began == SIM["plugin_max_s"]
    # try 2: the captcha button clicked by selector right away ⇒ detached element, the try is lost
    with pytest.raises(CamofoxHTTPError):
        client.click_selector("tab-1", sel.REFRESH_BUTTON)
    # try 3: the captcha is redrawn (button clicked later), a 5 s identification window sees no variant
    clock.t = client.refresh_visible_at
    client.click_selector("tab-1", sel.REFRESH_BUTTON)
    window_end = clock() + SIM["old_facts_window_s"]
    seen = []
    while clock() <= window_end:
        seen.append(geo.identify(client.evaluate_value("tab-1", sel.FACTS_JS, timeout=10)))
        clock.sleep(1.0)
    assert seen and all(variant is None for variant in seen)


# --- the fix: the real solver on the same scenario -------------------------------------------------------------


def _run(client, clock, sad, budget=240.0):
    report = CaptchaReport()
    outcome = solve_captcha(
        client,
        "tab-1",
        api_key=KEY,
        report=report,
        clock=clock,
        sleep=clock.sleep,
        rng=random.Random(631),
        deadline=clock() + budget,
        sadcaptcha=sad,
    )
    return outcome, report


def test_solver_resolves_the_run_631_scenario():
    clock = Clock()
    client = Run631Camofox(clock)
    sad = _sad()
    outcome, report = _run(client, clock, sad)
    assert outcome == "solved"
    assert len(sad.calls) == 2
    attempts = report.captchas[0]["attempts"]
    assert [(a["try"], a["type"], a["variant"], a["result"]) for a in attempts] == [
        (1, "rotate", "v2", "failed"),
        (2, "rotate", "v2", "solved"),
    ]
    # no 504, every request under the per-request budget, back to back with the button held
    assert all(r["status"] == 200 for r in client.requests)
    assert all(r["end"] - r["start"] <= geo.GESTURE_REQUEST_BUDGET_S for r in client.requests)
    # the captcha's button: looked up until it is attached again, clicked, then ≥ 3 s before the next read
    clicks = [t for t, name in client.log if name == "refresh_click"]
    assert len(clicks) == 1
    lookups = [t for t, name in client.log if name == "refresh_lookup"]
    assert lookups[-1] - lookups[0] >= SIM["refresh_detached_s_after_failure"] - 1
    next_facts = min(t for t, name in client.log if name == "facts" and t > clicks[0])
    assert next_facts - clicks[0] >= solver.PAUSE_AFTER_REFRESH_S
    # identification waited for the redrawn captcha (8 s of envelope without variant)
    facts_try2 = [t for t, name in client.log if name == "facts" and t > clicks[0]]
    assert facts_try2[-1] - clicks[0] >= SIM["blank_envelope_s_after_refresh"]
    first, second = report.diagnostics
    assert (first["step"], first["captcha_button"], first["http"]) == ("not_gone", "clicked", 200)
    assert first["angle"] == SIM["sadcaptcha_angle"] and first["requests"] >= 2
    assert second["step"] == "solved"
    lines = solver.diagnostic_lines(report)
    assert len(lines) == 2
    assert lines[0].startswith(f"captcha 1.1: variant=rotate_v2 step=not_gone angle={SIM['sadcaptcha_angle']} distance=")
    assert " http=200 refresh=clicked " in lines[0]
    assert lines[1].startswith("captcha 1.2: variant=rotate_v2 step=solved ")


def test_solver_still_too_slow_is_three_gesture_timeouts():
    clock = Clock()
    client = Run631Camofox(clock, move_cost_s=geo.GESTURE_REQUEST_BUDGET_S * 2)  # every request hits the ceiling
    client.verdicts = [[True], [True], [True]]
    sad = _sad()
    outcome, report = _run(client, clock, sad, budget=600.0)
    assert outcome == "unsolved"
    assert len(sad.calls) <= 3
    assert [d["step"] for d in report.diagnostics] == ["gesture_timeout"] * 3
    releases = [c for c in client.calls if c[0] == "mouse" and c[2] == [{"type": "wait", "ms": 300}, {"type": "up"}]]
    assert len(releases) == 3
    assert all(r["status"] == 504 for r in client.requests if r.get("actions") is None)
