"""s27 — the run 922 sequence (rotate v2, exit_18, three tries not_gone), replayed by a simulated Camofox (console ADR 061).

The fixture holds the three observed diagnostic lines (no content data), the geometry they impose (bar − handle =
284 px) and the simulation parameters. The scene is one where the s26 conversion cannot solve: the inner image
turns twice as fast as the 360°-over-the-travel assumption (k_in_ratio). The simulator first reproduces the three
observed tries with the s26 gesture (drag_actions played in one block), then the real solve_captcha is run on the
same scene, and on a scene that is never solved to check that the three paid tries fit in SOLVER_BUDGET_S.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from app import captcha_geometry as geo
from app import captcha_selectors as sel
from app import captcha_solver as solver
from app.captcha_solver import CaptchaReport, solve_captcha
from app.tiktok_scan import SOLVER_BUDGET_S
from test_captcha_solver import BOX_BAR, BOX_HANDLE, KEY, Clock, FakeCamofox, FakeSad

FIXTURE = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "captcha" / "run922_rotate_v2.json").read_text(encoding="utf-8")
)
SIM = FIXTURE["simulation"]
BAR = FIXTURE["geometry"]["bar_width"]
HANDLE = FIXTURE["geometry"]["handle_width"]
ANGLES = [o["angle"] for o in FIXTURE["observed"]]


class Run922Camofox(FakeCamofox):
    """Camofox as seen during run 922: the 284 px travel, humanised moves, costed reads, a scaled inner rotation."""

    def __init__(self, clock: Clock, *, solvable: bool = True) -> None:
        super().__init__("rotate_v2", clock, verdicts=[] if solvable else [[True]] * 3)
        self.bar_box = dict(BOX_BAR, width=float(BAR))
        self.handle_box = dict(BOX_HANDLE, width=float(HANDLE))
        self.move_cost_s = SIM["move_cost_s"]
        self.plugin_max_s = float(SIM["plugin_max_s"])
        self.probe_cost_s = SIM["probe_cost_s"]
        self.k_in = SIM["k_in_ratio"] * 360 / (BAR - HANDLE)
        self.solvable = solvable

    def evaluate_value(self, tab_id, expression, *, timeout=None):
        if expression == sel.FACTS_JS:
            self.clock.t += SIM["facts_cost_s"]
        elif any(expression == sel.image_js(s) for s in sel.image_selectors("rotate_v2")):
            self.clock.t += SIM["image_cost_s"]
        return super().evaluate_value(tab_id, expression, timeout=timeout)


class Run922Sad(FakeSad):
    """The three angles of run 922, in order; on a solvable scene each one is the captcha's true answer."""

    def __init__(self, clock: Clock, client: Run922Camofox) -> None:
        super().__init__()
        self.clock = clock
        self.client = client
        self.angles = list(ANGLES)

    def rotate(self, outer, inner):
        self.calls.append(("rotate", outer, inner))
        self.clock.t += SIM["sadcaptcha_cost_s"]
        angle = self.angles.pop(0)
        if self.client.solvable:
            self.client.solve_when = angle
        return angle


def test_fixture_holds_the_observed_tries():
    observed = FIXTURE["observed"]
    assert [(o["try"], o["step"]) for o in observed] == [(1, "not_gone"), (2, "not_gone"), (3, "not_gone")]
    assert ANGLES == [166, 148, 99]
    assert [o["distance"] for o in observed] == [130, 116, 78]
    assert all(o["requests"] == 2 and o["moves"] == 10 for o in observed)
    assert [o["refresh"] for o in observed] == ["clicked", "clicked", "-"]
    assert all(20_000 <= o["ms"] <= 24_000 for o in observed[:2]) and 19_000 <= observed[2]["ms"] <= 20_000
    assert BAR - HANDLE == 284
    for o in observed:
        assert geo.rotate_distance(BAR, HANDLE, o["angle"]) == o["distance"]
    # review m4: the simulation timings are calibrated on the run 922 logs, not measured
    assert "calibrated on the run 922 logs, not measured" in SIM["note"]
    text = json.dumps(FIXTURE).lower()
    for banned in ("http", "@", "sadkey", "base64", "licensekey"):
        assert banned not in text.replace("hermes-console", "")


# --- reproduction: the s26 gesture, played in one block, does not solve the scene -------------------------------


def test_simulator_reproduces_run_922():
    clock = Clock()
    client = Run922Camofox(clock)
    start = geo.drag_start(client.handle_box)
    for index, observed in enumerate(FIXTURE["observed"]):
        client.solve_when = observed["angle"]
        distance = geo.rotate_distance(BAR, HANDLE, observed["angle"])
        assert distance == observed["distance"]
        before = len(client.requests)
        began = clock()
        for chunk, hold in geo.split_gesture(geo.drag_actions(start, distance, random.Random(922 + index))):
            client.mouse("tab-1", chunk, hold=hold)
        played = client.requests[before:]
        assert len(played) == observed["requests"] == 2
        assert sum(1 for r in played for a in r["actions"] if a["type"] == "move") == observed["moves"]
        assert 10.0 <= clock() - began <= 16.0  # observed gesture_ms 10 693 - 14 610
        assert client.evaluate_value("tab-1", sel.PRESENT_JS, timeout=10) is True  # not_gone


# --- the fix: the real solver on the same scene -----------------------------------------------------------------


def _run(client, clock, sad, budget=SOLVER_BUDGET_S):
    report = CaptchaReport()
    outcome = solve_captcha(
        client,
        "tab-1",
        api_key=KEY,
        report=report,
        clock=clock,
        sleep=clock.sleep,
        rng=random.Random(922),
        deadline=clock() + budget,
        sadcaptcha=sad,
    )
    return outcome, report


def test_solver_corrects_the_run_922_scene_at_the_first_try():
    clock = Clock()
    client = Run922Camofox(clock)
    sad = Run922Sad(clock, client)
    outcome, report = _run(client, clock, sad)
    assert outcome == "solved"
    assert len(sad.calls) == 1
    (diag,) = report.diagnostics
    assert diag["step"] == "solved" and diag["fix"] == "moved"
    assert diag["requests"] == 3 and diag["moves"] == 11
    assert diag["distance"] == 130 and diag["target"] == round(166 / client.k_in)
    assert all(r["status"] == 200 for r in client.requests)
    line = solver.diagnostic_lines(report)[0]
    assert line.startswith("captcha 1.1: variant=rotate_v2 step=solved angle=166 distance=130 requests=3 moves=11 ")
    assert " bar=348 handle=64 " in line and " fix=moved sense=direct" in line


def test_three_paid_tries_fit_in_the_solver_budget():
    # requirement (P6): on a scene never solved, with the run 922 durations, the 3 tries reach SadCaptcha
    clock = Clock()
    client = Run922Camofox(clock, solvable=False)
    sad = Run922Sad(clock, client)
    outcome, report = _run(client, clock, sad, budget=240.0)
    assert SOLVER_BUDGET_S == 240.0
    assert outcome == "unsolved"
    assert len(sad.calls) == 3
    assert [d["step"] for d in report.diagnostics] == ["not_gone"] * 3
    lines = solver.diagnostic_lines(report)
    assert len(lines) == 3 and not any(" step=budget " in line for line in lines)
    assert all(18_000 <= d["ms"] <= 30_000 for d in report.diagnostics)  # ~20-24 s per try observed
