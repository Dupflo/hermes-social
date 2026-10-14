"""s22 — app/captcha_solver.py: bounded solver on a scripted fake Camofox client and a fake SadCaptcha.

Nothing reaches TikTok nor SadCaptcha: the only HTTP traffic is to 127.0.0.1 (image fallback, end-to-end case).
"""

from __future__ import annotations

import base64
import json
import random
import re
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app import captcha_geometry as geo
from app import captcha_selectors as sel
from app import captcha_solver as solver
from app.camofox_reader import CamofoxHTTPError, CamofoxTimeout, CamofoxUnreachable
from app.captcha_solver import CaptchaReport, solve_captcha
from app.sadcaptcha import SadCaptchaClient, SadCaptchaError

KEY = "SADKEY-SENTINEL-123"
FACT_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "captcha" / "facts"
BOX_HANDLE = {"x": 100.0, "y": 400.0, "width": 40.0, "height": 40.0}
BOX_PUZZLE = {"x": 80.0, "y": 150.0, "width": 340.0, "height": 212.0}
BOX_BAR = {"x": 80.0, "y": 400.0, "width": 340.0, "height": 40.0}
BOX_REFRESH = {"x": 300.0, "y": 120.0, "width": 24.0, "height": 24.0}


def facts_for(variant: str | None) -> dict:
    name = variant or "none"
    return json.loads((FACT_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["facts"]


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class FakeCamofox:
    """Scripted Camofox client. `verdicts`: one list of PRESENT_JS answers per try (True = still there).

    s25 (backward compatible): simulated plugin time — `move_cost_s` per move (× steps), waits, a per-request
    ceiling `plugin_max_s` (clock pushed to the ceiling, button released, CamofoxHTTPError(504)); button state
    followed across requests (`hold`); `requests` keeps start/end/hold/status of each /mouse request;
    `fail_requests` {n: status} fails the n-th /mouse call; `extra_cost` {n: s} slows it down; the captcha's
    REFRESH_JS rectangle (`refresh_rect`, visible from `refresh_visible_at`); `facts_at(t)` scripts the facts in time.

    s27 (console ADR 061, backward compatible): the mouse x and the drag origin (x at `down`) are followed; rotate v2
    geometry carries `probe`, its handle moved by `handle_follow` × (x − origin) while the button is held; inner and
    outer rotations `k_in` / `k_out` × moved, sources `rsrc_in` / `rsrc_out` (`none` ⇒ no rotation read); a read with
    the button held is logged `held_probe`, costs `probe_cost_s`, raises `probe_raises` (a CamofoxTimeout after its
    timeout); `solve_when` (an angle) decides PRESENT_JS on the final relative rotation, compared modulo 360 (± 3°).
    Review m1: `rest_in` / `rest_out` are the rotations shown before the drag (added to the held reading; `None` ⇒
    the reading before the drag has no rotation, the held one counts from 0).
    """

    def __init__(self, variant: str | None, clock: Clock, *, verdicts=None, available=True) -> None:
        self.variant = variant
        self.clock = clock
        self.available = available
        self.calls: list[tuple] = []
        self.facts = facts_for(variant)
        self.verdicts = list(verdicts if verdicts is not None else [[False, False]])
        self.current_verdicts: list = []
        self.images: dict[str, object] = {}
        self.translate_x = 0
        self.translate_step = 2
        self.translate_raises_after: int | None = None
        self.geometry_reads = 0
        self.raise_on: dict[str, BaseException] = {}
        self.mouse_step_ms = 0.0
        self.move_cost_s = 0.0
        self.plugin_max_s: float | None = None
        self.button_down = False
        self.requests: list[dict] = []
        self.fail_requests: dict[int, int] = {}
        self.extra_cost: dict[int, float] = {}
        self.refresh_rect: dict | None = dict(BOX_REFRESH)
        self.refresh_visible_at = float("-inf")
        self.facts_at = None
        self.log: list[tuple[float, str]] = []
        self.mouse_count = 0
        self.bar_box = dict(BOX_BAR)
        self.handle_box = dict(BOX_HANDLE)
        self.mouse_x: float | None = None
        self.drag_origin: float | None = None
        self.final_moved: float | None = None
        self.handle_follow = 1.0
        self.k_in = 360 / (BOX_BAR["width"] - BOX_HANDLE["width"])
        self.k_out = 0.0
        self.rsrc_in = "inline"
        self.rsrc_out = "none"
        self.probe_cost_s = 0.0
        self.probe_raises: BaseException | None = None
        self.solve_when: float | None = None
        self.same_image = False
        self.probe_extra: dict = {}
        self.rest_in: float | None = 0.0
        self.rest_out: float | None = 0.0

    # forbidden in the solver
    def navigate(self, *a, **k):
        raise AssertionError("navigate called")

    def delete_tab(self, *a, **k):
        raise AssertionError("delete_tab called")

    def press(self, *a, **k):
        raise AssertionError("press called")

    def type_text(self, *a, **k):
        raise AssertionError("type_text called")

    def mouse_available(self, *, timeout=5.0):
        self.calls.append(("mouse_available",))
        return self.available

    def mouse(self, tab_id, actions, *, hold=False, timeout=25.0):
        self.calls.append(("mouse", tab_id, actions, hold, timeout))
        self.mouse_count += 1
        start = self.clock.t
        self.log.append((start, "mouse"))
        if "mouse" in self.raise_on and not (len(actions) == 1 and actions[0]["type"] == "up"):
            raise self.raise_on["mouse"]
        if self.mouse_count in self.fail_requests:
            status = self.fail_requests[self.mouse_count]
            self.button_down = False  # the plugin releases before answering 504/500
            self.requests.append({"start": start, "end": self.clock.t, "hold": hold, "status": status, "down": False})
            raise CamofoxHTTPError(status)
        if self.variant == "puzzle_v2" and hold and len(actions) == 1 and actions[0]["type"] == "move":
            self.translate_x += self.translate_step
        cost = self.extra_cost.get(self.mouse_count, 0.0)
        for action in actions:
            if action["type"] == "move":
                cost += self.move_cost_s * action.get("steps", 1)
            elif action["type"] == "wait":
                cost += action["ms"] / 1000
        if self.plugin_max_s is not None and cost > self.plugin_max_s:
            self.clock.t += self.plugin_max_s
            self.button_down = False
            self.requests.append({"start": start, "end": self.clock.t, "hold": hold, "status": 504, "down": False})
            raise CamofoxHTTPError(504)
        self.clock.t += cost + self.mouse_step_ms / 1000
        for action in actions:
            if action["type"] == "move":
                self.mouse_x = action["x"]
            elif action["type"] == "down":
                self.button_down = True
                self.drag_origin = self.mouse_x
            elif action["type"] == "up":
                if self.button_down:
                    self.final_moved = self.held_moved()
                self.button_down = False
        if self.button_down and not hold:
            self.final_moved = self.held_moved()
            self.button_down = False  # end of a request without hold: the plugin releases
        self.requests.append(
            {"start": start, "end": self.clock.t, "hold": hold, "status": 200, "down": self.button_down, "actions": actions}
        )
        if self.is_refresh_click(actions):
            self.log.append((self.clock.t, "refresh_click"))
            self.refresh_clicked()
        return len(actions)

    def is_refresh_click(self, actions):
        moves = [a for a in actions if a["type"] == "move"]
        if self.refresh_rect is None or len(moves) != 1:
            return False
        box = self.refresh_rect
        return (moves[0]["x"], moves[0]["y"]) == (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)

    def refresh_clicked(self):
        return None

    def click_selector(self, tab_id, selector, *, timeout=None):
        self.calls.append(("click", selector))

    def held_moved(self) -> float:
        if self.mouse_x is None or self.drag_origin is None:
            return 0.0
        return self.handle_follow * (self.mouse_x - self.drag_origin)

    def _rotation(self, k: float, rest: float | None, moved: float, held: bool) -> float | None:
        if not held:
            return rest
        return (rest or 0.0) + k * moved

    def _probe(self, moved: float, held: bool) -> dict:
        probe = {
            "track": dict(self.bar_box),
            "inner": {"x": 150.0, "y": 200.0, "width": 110.0, "height": 110.0},
            "outer": {"x": 110.0, "y": 160.0, "width": 190.0, "height": 190.0},
            "in_nat": 220,
            "out_nat": 380,
            "rot_in": self._rotation(self.k_in, self.rest_in, moved, held) if self.rsrc_in != "none" else None,
            "rot_out": self._rotation(self.k_out, self.rest_out, moved, held) if self.rsrc_out != "none" else None,
            "rsrc_in": self.rsrc_in,
            "rsrc_out": self.rsrc_out,
            "pair": "same" if self.same_image else "ok",
        }
        probe.update(self.probe_extra)
        return probe

    def _geometry(self):
        kind, _ = geo.variant_parts(self.variant)
        if kind == "puzzle":
            rects = {"puzzle": BOX_PUZZLE, "handle": BOX_HANDLE}
        elif kind == "rotate":
            rects = {"bar": self.bar_box, "handle": self.handle_box}
        else:
            rects = {"image": BOX_PUZZLE}
        out = {"rects": rects}
        if self.variant == "rotate_v2":
            moved = self.held_moved() if self.button_down else 0.0
            out["rects"] = {"bar": dict(self.bar_box), "handle": dict(self.handle_box, x=self.handle_box["x"] + moved)}
            out["probe"] = self._probe(moved, self.button_down)
        if self.variant == "puzzle_v2":
            self.geometry_reads += 1
            if self.translate_raises_after is not None and self.geometry_reads > self.translate_raises_after:
                raise CamofoxHTTPError(500)
            out["translate_x"] = self.translate_x
        return out

    def evaluate_value(self, tab_id, expression, *, timeout=None):
        assert timeout is not None and timeout <= 10
        if expression == sel.PRESENT_JS:
            self.calls.append(("present",))
            if self.solve_when is not None:
                relative = (self.k_in - self.k_out) * (self.final_moved or 0.0)
                gap = (relative - self.solve_when) % 360
                return not min(gap, 360 - gap) <= 3
            if not self.current_verdicts:
                self.current_verdicts = list(self.verdicts.pop(0)) if self.verdicts else [True]
            value = self.current_verdicts.pop(0) if len(self.current_verdicts) > 1 else self.current_verdicts[0]
            return value
        if expression == sel.FACTS_JS:
            self.calls.append(("facts",))
            self.log.append((self.clock.t, "facts"))
            if "facts" in self.raise_on:
                raise self.raise_on["facts"]
            self.current_verdicts = []
            return self.facts_at(self.clock.t) if self.facts_at is not None else self.facts
        if expression == sel.REFRESH_JS:
            self.calls.append(("refresh_lookup",))
            self.log.append((self.clock.t, "refresh_lookup"))
            if self.refresh_rect is None or self.clock.t < self.refresh_visible_at:
                return None
            return dict(self.refresh_rect)
        if self.variant == "rotate_v2" and self.button_down and expression == sel.geometry_js(self.variant):
            self.calls.append(("held_probe",))
            self.log.append((self.clock.t, "held_probe"))
            if self.probe_raises is not None:
                if isinstance(self.probe_raises, CamofoxTimeout):
                    self.clock.t += timeout
                raise self.probe_raises
            self.clock.t += self.probe_cost_s
            return self._geometry()
        if self.variant and expression == sel.geometry_js(self.variant):
            self.calls.append(("geometry",))
            return self._geometry()
        for selector in sel.image_selectors(self.variant) if self.variant else ():
            if expression == sel.image_js(selector):
                self.calls.append(("image", selector))
                return self.images.get(selector, {"b64": base64.b64encode(selector.encode()).decode()})
        raise AssertionError("unexpected expression")


class FakeSad:
    def __init__(self, answers=None, error: SadCaptchaError | None = None) -> None:
        self.calls: list[tuple] = []
        self.error = error
        self.answers = answers or {
            "rotate": 90,
            "puzzle": 0.5,
            "shapes": ((0.25, 0.5), (0.75, 0.1)),
            "icon": [(0.1, 0.2), (0.5, 0.5)],
        }

    def _answer(self, kind, *args):
        self.calls.append((kind, *args))
        if self.error:
            raise self.error
        return self.answers[kind]

    def rotate(self, outer, inner):
        return self._answer("rotate", outer, inner)

    def puzzle(self, puzzle, piece):
        return self._answer("puzzle", puzzle, piece)

    def shapes(self, image):
        return self._answer("shapes", image)

    def icon(self, challenge, image):
        return self._answer("icon", challenge, image)


def run(client, clock, *, sad=None, report=None, key=KEY, budget=240.0):
    report = report if report is not None else CaptchaReport()
    outcome = solve_captcha(
        client,
        "tab-1",
        api_key=key,
        report=report,
        clock=clock,
        sleep=clock.sleep,
        rng=random.Random(5),
        deadline=clock() + budget,
        sadcaptcha=sad,
    )
    return outcome, report


def mouse_calls(client):
    return [c for c in client.calls if c[0] == "mouse"]


@pytest.mark.parametrize("variant", geo.VARIANTS)
def test_each_variant_is_solved_at_the_first_try(variant):
    clock = Clock()
    client = FakeCamofox(variant, clock)
    sad = FakeSad()
    outcome, report = run(client, clock, sad=sad)
    assert outcome == "solved"
    kind, version = geo.variant_parts(variant)
    assert [c[0] for c in sad.calls] == [kind]
    captcha = report.captchas[0]
    assert captcha["outcome"] == "solved" and captcha["seq"] == 1
    assert [(a["try"], a["type"], a["variant"], a["result"]) for a in captcha["attempts"]] == [(1, kind, version, "solved")]
    mouses = mouse_calls(client)
    clicks = [c for c in client.calls if c[0] == "click"]
    if kind in ("rotate",) or variant == "puzzle_v1":
        # s25 : épingle amendée (geste découpé bouton tenu, console ADR 059) — was one request, hold False
        assert len(mouses) >= 2
        assert [c[3] for c in mouses] == [True] * (len(mouses) - 1) + [False]
        actions = [a for c in mouses for a in c[2]]
        moves = [a for a in actions if a["type"] == "move"]
        start = geo.drag_start(BOX_HANDLE)
        if kind == "rotate":
            distance = geo.rotate_distance(BOX_BAR["width"], BOX_HANDLE["width"], 90)
        else:
            distance = geo.puzzle_distance(0.5, BOX_PUZZLE["width"])
        assert moves[-1]["x"] == pytest.approx(start[0] + distance)
        assert [a["type"] for a in actions].count("down") == 1
        assert clicks == []
    elif variant == "puzzle_v2":
        assert mouses[0][3] is True  # hold
        assert mouses[-1][2] == [{"type": "wait", "ms": 300}, {"type": "up"}]
        target = geo.puzzle_distance(0.5, BOX_PUZZLE["width"])
        assert client.translate_x >= target - 1
        assert clicks == []
    else:
        assert len(mouses) == 1
        actions = mouses[0][2]
        points = [(a["x"], a["y"]) for a in actions if a["type"] == "move"]
        expected = 2 if kind == "shapes" else 2
        assert len(points) == expected
        assert [a["type"] for a in actions].count("down") == expected
        assert clicks == [("click", sel.ELEMENTS[variant]["submit"])]
    if kind == "icon":
        assert sad.calls[0][1] == facts_for(variant)["challenge"]


def test_failed_then_solved():
    clock = Clock()
    client = FakeCamofox("rotate_v1", clock, verdicts=[[True], [False, False]])
    outcome, report = run(client, clock, sad=FakeSad())
    assert outcome == "solved"
    results = [a["result"] for a in report.captchas[0]["attempts"]]
    assert results == ["failed", "solved"]


@pytest.mark.parametrize("variant", ["rotate_v2", "rotate_v1", "puzzle_v1", "shapes_v2"])
def test_three_failures_are_unsolved_and_only_rotate_v2_refreshes(variant):
    clock = Clock()
    client = FakeCamofox(variant, clock, verdicts=[[True], [True], [True]])
    sad = FakeSad()
    outcome, report = run(client, clock, sad=sad)
    assert outcome == "unsolved"
    assert [a["try"] for a in report.captchas[0]["attempts"]] == [1, 2, 3]
    assert all(a["result"] == "failed" for a in report.captchas[0]["attempts"])
    assert len(sad.calls) == 3
    # s25 : épingle amendée (rafraîchissement relu, console ADR 059): REFRESH_JS read, then a plugin click at
    # the centre of the rectangle just read; never a click by selector; ≥ 3 s before the next facts read.
    assert [c for c in client.calls if c == ("click", sel.REFRESH_BUTTON)] == []
    clicks = [t for t, name in client.log if name == "refresh_click"]
    assert len(clicks) == (2 if variant == "rotate_v2" else 0)
    lookups = [c for c in client.calls if c == ("refresh_lookup",)]
    assert (len(lookups) >= 2) if variant == "rotate_v2" else (lookups == [])
    for clicked in clicks:
        next_facts = min(t for t, name in client.log if name == "facts" and t >= clicked)
        assert next_facts - clicked >= solver.PAUSE_AFTER_REFRESH_S


def test_unrecognized_after_thirty_seconds_of_facts():
    # s25 : épingle amendée (fenêtre d'identification de 30 s, console ADR 059) — was 5 s, 2-6 reads
    clock = Clock()
    client = FakeCamofox(None, clock)
    sad = FakeSad()
    start = clock()
    outcome, report = run(client, clock, sad=sad)
    assert outcome == "unrecognized"
    assert sad.calls == []
    assert mouse_calls(client) == []
    assert report.captchas[0]["attempts"][0]["type"] == "unknown"
    assert report.captchas[0]["attempts"][0]["variant"] == "none"
    assert report.captchas[0]["attempts"][0]["result"] == "unrecognized"
    facts_reads = [c for c in client.calls if c == ("facts",)]
    assert len(facts_reads) <= 31
    assert clock() - start >= 30
    assert solver.FACTS_WINDOW_S == 30.0


def test_identification_waits_for_a_captcha_being_reloaded():
    clock = Clock()
    client = FakeCamofox("rotate_v2", clock)
    blank = dict(facts_for(None), present=True)
    shown_at = clock() + 8
    client.facts_at = lambda t: blank if t < shown_at else facts_for("rotate_v2")
    outcome, _ = run(client, clock, sad=FakeSad())
    assert outcome == "solved"


@pytest.mark.parametrize("variant", ["rotate_v2", "rotate_v1", "puzzle_v1"])
def test_drag_is_split_with_the_button_held_and_no_gap(variant):
    clock = Clock()
    client = FakeCamofox(variant, clock)
    client.move_cost_s = geo.HUMANIZE_MOVE_MAX_S
    client.plugin_max_s = geo.PLUGIN_MAX_RUNTIME_S
    client.probe_cost_s = 0.4
    outcome, report = run(client, clock, sad=FakeSad())
    assert outcome == "solved"
    requests = client.requests
    assert len(requests) >= 2
    assert all(r["status"] == 200 for r in requests)
    assert [r["hold"] for r in requests] == [True] * (len(requests) - 1) + [False]
    assert [r["down"] for r in requests] == [True] * (len(requests) - 1) + [False]
    # s27 : épingle amendée (lecture tenue du rotate v2, console ADR 061) — rotate v2 has ONE gap, the held read,
    # between its last held request and the final one; rotate v1 and puzzle v1 keep no gap at all.
    gaps = [after["start"] - before["end"] for before, after in zip(requests, requests[1:])]
    if variant == "rotate_v2":
        assert gaps[:-1] == [0] * (len(gaps) - 1)
        assert 0 < gaps[-1] <= solver.HELD_PROBE_TIMEOUT_S + client.probe_cost_s
        assert gaps[-1] < geo.PLUGIN_HOLD_RELEASE_S
        assert requests[-1]["actions"] == [{"type": "up"}]
    else:
        assert gaps == [0] * len(gaps)
    for r in requests:
        assert r["end"] - r["start"] <= geo.GESTURE_REQUEST_BUDGET_S
    assert client.button_down is False
    diag = report.diagnostics[0]
    assert diag["step"] == "solved" and diag["requests"] == len(requests) and diag["http"] == 200
    assert diag["moves"] == sum(1 for r in requests for a in r["actions"] if a["type"] == "move")
    if variant.startswith("rotate"):
        assert diag["angle"] == 90
        assert diag["distance"] == geo.rotate_distance(BOX_BAR["width"], BOX_HANDLE["width"], 90)
    else:
        assert diag["angle"] is None and diag["distance"] == geo.puzzle_distance(0.5, BOX_PUZZLE["width"])


@pytest.mark.parametrize(("status", "step"), [(504, "gesture_timeout"), (500, "gesture_error")])
def test_failed_request_mid_gesture_stops_settles_and_releases(status, step):
    clock = Clock()
    client = FakeCamofox("rotate_v2", clock, verdicts=[[False, False]])  # try 1 never reaches its check
    client.fail_requests = {2: status}  # second request of the first gesture
    outcome, report = run(client, clock, sad=FakeSad())
    assert outcome == "solved"
    mouses = mouse_calls(client)
    assert mouses[0][3] is True  # first request, button held
    release = mouses[2]
    assert release[2] == [{"type": "wait", "ms": 300}, {"type": "up"}]
    times = [t for t, name in client.log if name == "mouse"]
    assert times[2] - times[1] == solver.GESTURE_SETTLE_S
    diag = report.diagnostics[0]
    assert diag["step"] == step and diag["http"] == status and diag["requests"] == 2
    assert [a["result"] for a in report.captchas[0]["attempts"]] == ["failed", "solved"]


def test_plugin_ceiling_is_a_gesture_timeout_and_three_of_them_are_unsolved():
    clock = Clock()
    client = FakeCamofox("rotate_v2", clock, verdicts=[[True], [True], [True]])
    client.move_cost_s = 4.0  # every request reaches the plugin ceiling
    client.plugin_max_s = geo.PLUGIN_MAX_RUNTIME_S
    sad = FakeSad()
    outcome, report = run(client, clock, sad=sad, budget=600.0)
    assert outcome == "unsolved"
    assert len(sad.calls) == 3
    assert [d["step"] for d in report.diagnostics] == ["gesture_timeout"] * 3
    assert all(d["http"] == 504 and d["requests"] == 1 for d in report.diagnostics)


def test_missing_captcha_button_means_no_click_but_still_the_pause():
    clock = Clock()
    client = FakeCamofox("rotate_v2", clock, verdicts=[[True], [False, False]])
    client.refresh_rect = None
    outcome, report = run(client, clock, sad=FakeSad())
    assert outcome == "solved"
    assert [t for t, name in client.log if name == "refresh_click"] == []
    lookups = [t for t, name in client.log if name == "refresh_lookup"]
    assert 1 <= len(lookups) <= 6
    assert lookups[-1] - lookups[0] <= solver.REFRESH_LOOKUP_S
    next_facts = min(t for t, name in client.log if name == "facts" and t > lookups[-1])
    assert next_facts - lookups[-1] >= solver.PAUSE_AFTER_REFRESH_S
    assert report.diagnostics[0]["captcha_button"] == "missing"
    assert report.diagnostics[1]["captcha_button"] is None


def test_captcha_button_clicked_at_the_rectangle_read_again():
    clock = Clock()
    client = FakeCamofox("rotate_v2", clock, verdicts=[[True], [False, False]])
    moved = {"x": 500.0, "y": 60.0, "width": 30.0, "height": 20.0}
    original = client.evaluate_value

    def evaluate(tab_id, expression, *, timeout=None):
        if expression == sel.REFRESH_JS and client.refresh_rect == BOX_REFRESH:
            # first lookup: the button seen before the failure is gone; a new one shows up 3 s later, elsewhere
            client.refresh_rect = moved
            client.refresh_visible_at = clock() + 3
        return original(tab_id, expression, timeout=timeout)

    client.evaluate_value = evaluate
    outcome, report = run(client, clock, sad=FakeSad())
    assert outcome == "solved"
    clicks = [r for r in client.requests if r.get("actions") and client.is_refresh_click(r["actions"])]
    assert len(clicks) == 1
    move = [a for a in clicks[0]["actions"] if a["type"] == "move"][0]
    assert (move["x"], move["y"]) == (515.0, 70.0)
    lookups = [t for t, name in client.log if name == "refresh_lookup"]
    assert lookups[-1] - lookups[0] >= 3
    assert report.diagnostics[0]["captcha_button"] == "clicked"


class SlowSad(FakeSad):
    def __init__(self, clock, **kwargs):
        super().__init__(**kwargs)
        self.clock = clock

    def _answer(self, kind, *args):
        assert self.deadline - self.clock.t >= solver.SADCAPTCHA_TIMEOUT_S
        self.clock.t += solver.SADCAPTCHA_TIMEOUT_S
        return super()._answer(kind, *args)


class SlowCamofox(FakeCamofox):
    """Every call consumes its maximal delay; a call never starts without that delay before the deadline."""

    deadline = float("inf")

    def _spend(self, seconds):
        assert self.deadline - self.clock.t >= seconds - 1e-9
        self.clock.t += seconds

    def evaluate_value(self, tab_id, expression, *, timeout=None):
        self._spend(timeout)
        return super().evaluate_value(tab_id, expression, timeout=timeout)

    def mouse(self, tab_id, actions, *, hold=False, timeout=25.0):
        self._spend(timeout)
        return super().mouse(tab_id, actions, hold=hold, timeout=timeout)

    def click_selector(self, tab_id, selector, *, timeout=None):
        self._spend(timeout)
        return super().click_selector(tab_id, selector, timeout=timeout)


@pytest.mark.parametrize("variant", ["rotate_v2", "rotate_v1", "puzzle_v1", "shapes_v2", "icon_v1", None])
@pytest.mark.parametrize("budget", range(60, 401, 20))
def test_solver_hands_back_by_its_deadline(variant, budget):
    clock = Clock()
    client = SlowCamofox(variant, clock, verdicts=[[True]] * 3)
    sad = SlowSad(clock)
    deadline = clock() + budget
    client.deadline = sad.deadline = deadline
    report = CaptchaReport()
    solve_captcha(
        client, "tab-1", api_key=KEY, report=report, clock=clock, sleep=clock.sleep,
        rng=random.Random(5), deadline=deadline, sadcaptcha=sad,
    )
    assert clock() <= deadline


def test_no_paid_call_without_the_budget_to_play_its_answer():
    clock = Clock()
    client = FakeCamofox("rotate_v2", clock, verdicts=[[True], [True], [True]])
    sad = FakeSad()
    outcome, report = run(client, clock, sad=sad, budget=100.0)
    assert sad.calls == []
    assert len([c for c in client.calls if c[0] == "image"]) == 2
    assert report.diagnostics[0]["step"] == "budget"
    assert outcome == "unsolved"
    assert mouse_calls(client) == []


def test_a_started_gesture_is_played_to_the_end_near_the_deadline():
    clock = Clock()
    client = FakeCamofox("rotate_v2", clock)
    client.extra_cost = {1: 140.0}  # the first request leaves less than one request's timeout
    client.probe_cost_s = 0.5
    start = clock()
    outcome, report = run(client, clock, sad=FakeSad(), budget=160.0)
    requests = client.requests
    assert len(requests) >= 2
    assert start + 160.0 - requests[0]["end"] < solver.MOUSE_TIMEOUT_S
    # s27 : épingle amendée (lecture tenue du rotate v2, console ADR 061) — only the held read's single gap, before
    # the final request; nothing else between two requests of the gesture.
    gaps = [after["start"] - before["end"] for before, after in zip(requests, requests[1:])]
    assert gaps[:-1] == [0] * (len(gaps) - 1)
    assert gaps[-1] == pytest.approx(client.probe_cost_s)
    assert report.diagnostics[0]["requests"] == len(requests)


@pytest.mark.parametrize("key", ["", "   \n"])
def test_disabled_without_key(key):
    clock = Clock()
    client = FakeCamofox("rotate_v1", clock)
    sad = FakeSad()
    outcome, report = run(client, clock, sad=sad, key=key)
    assert outcome == "disabled"
    assert client.calls == [] and sad.calls == []
    assert report.captchas == [{"seq": 1, "outcome": "disabled", "at": int(clock() * 1000), "attempts": []}]


def test_unavailable_when_the_plugin_probe_fails():
    clock = Clock()
    client = FakeCamofox("rotate_v1", clock, available=False)
    sad = FakeSad()
    outcome, report = run(client, clock, sad=sad)
    assert outcome == "unavailable"
    assert sad.calls == []
    assert client.calls == [("mouse_available",)]
    assert report.captchas[0]["attempts"] == []


def test_unreachable_camofox_is_unavailable():
    clock = Clock()
    client = FakeCamofox("rotate_v1", clock)
    client.raise_on["facts"] = CamofoxUnreachable()
    outcome, _ = run(client, clock, sad=FakeSad())
    assert outcome == "unavailable"


def test_service_error_stops_the_run():
    clock = Clock()
    client = FakeCamofox("rotate_v1", clock)
    sad = FakeSad(error=SadCaptchaError("http"))
    report = CaptchaReport()
    outcome, _ = run(client, clock, sad=sad, report=report)
    assert outcome == "service_error"
    assert report.stop_run is True
    assert report.captchas[0]["attempts"] == []  # the try in progress is not logged
    assert len(sad.calls) == 1
    # second captcha of the run: nothing at all
    client2 = FakeCamofox("rotate_v1", clock)
    sad2 = FakeSad()
    outcome2, _ = run(client2, clock, sad=sad2, report=report)
    assert outcome2 == "service_error"
    assert client2.calls == [] and sad2.calls == []
    assert [c["outcome"] for c in report.captchas] == ["service_error", "service_error"]
    assert [c["seq"] for c in report.captchas] == [1, 2]


def test_no_try_under_sixty_seconds_of_budget():
    clock = Clock()
    client = FakeCamofox("rotate_v1", clock)
    sad = FakeSad()
    outcome, report = run(client, clock, sad=sad, budget=59.0)
    assert outcome == "unavailable"
    assert sad.calls == [] and report.captchas[0]["attempts"] == []


def test_budget_runs_out_after_one_try_is_unsolved():
    clock = Clock()
    client = FakeCamofox("rotate_v1", clock, verdicts=[[True], [True], [True]])
    sad = FakeSad()
    outcome, report = run(client, clock, sad=sad, budget=75.0)
    assert outcome == "unsolved"
    assert 1 <= len(report.captchas[0]["attempts"]) < 3


def test_puzzle_v2_stops_at_the_target():
    clock = Clock()
    client = FakeCamofox("puzzle_v2", clock)
    run(client, clock, sad=FakeSad())
    target = geo.puzzle_distance(0.5, BOX_PUZZLE["width"])
    steps = [c for c in mouse_calls(client) if c[3] is True and len(c[2]) == 1]
    assert len(steps) == -(-(target - 1) // 2)  # stopped as soon as translate_x ≥ target − 1
    assert steps[0][2][0]["x"] == pytest.approx(geo.drag_start(BOX_HANDLE)[0] + 2)


def test_puzzle_v2_releases_the_button_even_when_reading_fails():
    clock = Clock()
    client = FakeCamofox("puzzle_v2", clock, verdicts=[[True], [True], [True]])
    client.translate_raises_after = 3
    outcome, report = run(client, clock, sad=FakeSad())
    assert report.captchas[0]["attempts"][0]["result"] == "failed"
    mouses = mouse_calls(client)
    ups = [c for c in mouses if c[2][-1]["type"] == "up"]
    assert len(ups) >= 1
    assert outcome in ("unsolved", "solved")


def test_puzzle_v2_is_bounded_to_200_iterations():
    clock = Clock()
    client = FakeCamofox("puzzle_v2", clock, verdicts=[[True], [True], [True]])
    client.translate_step = 0  # the piece never moves
    run(client, clock, sad=FakeSad())
    per_try = [c for c in mouse_calls(client) if c[3] is True and len(c[2]) == 1]
    assert len(per_try) <= 3 * 200
    first_try = per_try[:200]
    assert len(first_try) == 200


def test_puzzle_v2_is_bounded_to_45_seconds():
    clock = Clock()
    client = FakeCamofox("puzzle_v2", clock, verdicts=[[True]])
    client.translate_step = 0
    client.mouse_step_ms = 500  # 0.5 s per step ⇒ 45 s reached before 200 iterations
    run(client, clock, sad=FakeSad(), budget=70)
    steps = [c for c in mouse_calls(client) if c[3] is True and len(c[2]) == 1]
    assert 85 <= len(steps) <= 92


def test_mouse_http_error_is_a_failed_try():
    clock = Clock()
    client = FakeCamofox("rotate_v1", clock, verdicts=[[True], [True], [True]])
    client.raise_on["mouse"] = CamofoxHTTPError(404)
    outcome, report = run(client, clock, sad=FakeSad())
    assert outcome == "unsolved"
    assert [a["result"] for a in report.captchas[0]["attempts"]] == ["failed"] * 3


def test_invalid_box_is_a_failed_try(monkeypatch):
    clock = Clock()
    client = FakeCamofox("shapes_v1", clock, verdicts=[[True], [True], [True]])
    monkeypatch.setattr(client, "_geometry", lambda: {"rects": {"image": {"x": 0, "y": 0, "width": 0, "height": 1}}})
    outcome, report = run(client, clock, sad=FakeSad())
    assert outcome == "unsolved"
    assert mouse_calls(client) == []


class _ImageServer:
    def __init__(self) -> None:
        self.hits: list[dict] = []
        hits = self.hits

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args, **kwargs):
                return

            def do_GET(self):
                hits.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}})
                body = b"PNGDATA"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture()
def image_server(monkeypatch):
    server = _ImageServer()
    real_urlopen = urllib.request.urlopen

    def redirected(request, timeout=None):
        url = request.full_url if isinstance(request, urllib.request.Request) else request
        path = url.split("/", 3)[3]
        local = urllib.request.Request(server.base + "/" + path, headers=dict(request.header_items()))
        return real_urlopen(local, timeout=timeout)

    monkeypatch.setattr(solver, "_urlopen", redirected)
    try:
        yield server
    finally:
        server.close()


def test_image_fallback_on_an_allowed_host(image_server):
    clock = Clock()
    client = FakeCamofox("shapes_v1", clock)
    selector = sel.ELEMENTS["shapes_v1"]["image"]
    client.images[selector] = {"error": "fetch", "src": "https://p19-rc-captcha-va.ibyteimg.com/obj/3d/x.png"}
    sad = FakeSad()
    outcome, _ = run(client, clock, sad=sad)
    assert outcome == "solved"
    assert image_server.hits and image_server.hits[0]["path"] == "/obj/3d/x.png"
    assert "cookie" not in image_server.hits[0]["headers"]
    assert sad.calls[0] == ("shapes", base64.b64encode(b"PNGDATA").decode())


@pytest.mark.parametrize(
    "src",
    [
        "https://evil.example/obj/3d/x.png",
        "http://p19-rc-captcha-va.ibyteimg.com/obj/3d/x.png",
        "https://ibyteimg.com.evil.example/x.png",
        "blob:https://www.tiktok.com/abc",
    ],
)
def test_image_fallback_refused_elsewhere(image_server, src):
    clock = Clock()
    client = FakeCamofox("shapes_v1", clock, verdicts=[[True], [True], [True]])
    client.images[sel.ELEMENTS["shapes_v1"]["image"]] = {"error": "fetch", "src": src}
    sad = FakeSad()
    outcome, report = run(client, clock, sad=sad)
    assert outcome == "unsolved"
    assert image_server.hits == []
    assert sad.calls == []
    assert [a["result"] for a in report.captchas[0]["attempts"]] == ["failed"] * 3


def test_end_to_end_with_the_http_sadcaptcha_client():
    answers = {"rotate": {"angle": 90}}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args, **kwargs):
            return

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = json.dumps(answers["rotate"]).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        clock = Clock()
        client = FakeCamofox("rotate_v2", clock)
        sad = SadCaptchaClient(KEY, base_url=f"http://127.0.0.1:{httpd.server_address[1]}/api/v1")
        outcome, _ = run(client, clock, sad=sad)
        assert outcome == "solved"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_report_json_follows_adr_052():
    clock = Clock()
    report = CaptchaReport()
    for verdicts, variant in (([[True], [False, False]], "rotate_v1"), ([[False, False]], "icon_v2")):
        client = FakeCamofox(variant, clock, verdicts=verdicts)
        run(client, clock, sad=FakeSad(), report=report)
    run(FakeCamofox("rotate_v1", clock), clock, sad=FakeSad(), report=report, key="")
    data = report.to_json()
    json.dumps(data)
    assert len(data) == 3
    for captcha in data:
        assert set(captcha) == {"seq", "outcome", "at", "attempts"}
        assert captcha["outcome"] in solver.CAPTCHA_OUTCOMES
        assert isinstance(captcha["at"], int)
        for attempt in captcha["attempts"]:
            assert set(attempt) == {"try", "type", "variant", "result", "ms"}
            assert attempt["type"] in ("rotate", "puzzle", "shapes", "icon", "unknown")
            assert attempt["variant"] in ("v1", "v2", "none")
            assert attempt["result"] in ("solved", "failed", "unrecognized")
            assert isinstance(attempt["ms"], int) and 0 <= attempt["ms"] <= 600000
    assert [c["seq"] for c in data] == [1, 2, 3]
    assert data[0]["attempts"][1]["type"] == "rotate" and data[0]["attempts"][1]["variant"] == "v1"


LINE = re.compile(
    r"^captcha \d+\.\d+: variant=(puzzle_v1|puzzle_v2|rotate_v1|rotate_v2|shapes_v1|shapes_v2|icon_v1|icon_v2|unknown)"
    r" step=(solved|not_gone|identify|unrecognized|images|sadcaptcha|rects|gesture_timeout|gesture_error|budget|unavailable)"
    r" angle=(\d+|-) distance=(-?\d+|-) requests=\d+ moves=\d+ gesture_ms=\d+ http=(\d{3}|-)"
    r" refresh=(clicked|missing|error|-) ms=\d+"
    # s27 : épingle amendée (champs de mesure du rotate v2 en fin de ligne, console ADR 061)
    r" bar=(-?\d+|-) handle=(-?\d+|-) track=(-?\d+|-) in_w=(-?\d+|-) in_nat=(-?\d+|-) out_w=(-?\d+|-)"
    r" pair=(ok|same|-) moved=(-?\d+|-) rot_in=(-?\d+|-) rot_out=(-?\d+|-)"
    r" rsrc=(inline|matrix|parent|none|-) rsrc_out=(inline|matrix|parent|none|-) k1000=(-?\d+|-) target=(-?\d+|-)"
    r" fix=(none|moved|no_read|no_rot|no_move|implausible|ambiguous|-) sense=(direct|complement|-)"
    r" rot0_in=(-?\d+|-) rot0_out=(-?\d+|-) rot0=(read|missing|-)$"
)


def test_diagnostic_lines_have_a_closed_format(image_server):
    clock = Clock()
    report = CaptchaReport()
    scenarios = [
        ("rotate_v2", [[True], [False, False]], {}),
        ("rotate_v2", [[True], [True], [True]], {"fail_requests": {1: 504}}),
        ("shapes_v1", [[True], [True], [True]], {"images_fallback": True}),
        (None, None, {}),
    ]
    for variant, verdicts, options in scenarios:
        client = FakeCamofox(variant, clock, verdicts=verdicts)
        client.fail_requests = options.get("fail_requests", {})
        if options.get("images_fallback"):
            selector = sel.ELEMENTS["shapes_v1"]["image"]
            client.images[selector] = {"error": "fetch", "src": "https://p19-rc-captcha-va.ibyteimg.com/obj/3d/SENTINEL_IMG.png"}
        run(client, clock, sad=FakeSad(), report=report)
    run(FakeCamofox("rotate_v1", clock), clock, sad=FakeSad(error=SadCaptchaError("http")), report=report)
    unreachable = FakeCamofox("rotate_v1", clock)
    unreachable.raise_on["facts"] = CamofoxUnreachable()
    run(unreachable, clock, sad=FakeSad(), report=CaptchaReport())
    lines = solver.diagnostic_lines(report)
    assert len(lines) == len(report.diagnostics) >= 8
    for line in lines:
        assert LINE.fullmatch(line), line
    assert lines[0].startswith("captcha 1.1: variant=rotate_v2 step=not_gone angle=90 distance=75 requests=")
    assert " refresh=clicked " in lines[0]
    assert lines[1].startswith("captcha 1.2: variant=rotate_v2 step=solved ")
    assert "captcha 2.1: variant=rotate_v2 step=gesture_timeout" in lines[2] and " http=504 " in lines[2]
    steps = [line.split(" step=")[1].split(" ")[0] for line in lines]
    assert "unrecognized" in steps and "sadcaptcha" in steps
    text = "\n".join(lines)
    for sentinel in (KEY, "SENTINEL_IMG", "ibyteimg", "http://", "https://", base64.b64encode(b"PNGDATA").decode(), "camofox http status"):
        assert sentinel not in text


def test_an_interrupted_try_has_its_line():
    clock = Clock()
    client = FakeCamofox("rotate_v1", clock)
    client.raise_on["facts"] = CamofoxUnreachable()
    _, report = run(client, clock, sad=FakeSad())
    assert report.captchas[0]["attempts"] == []
    assert [line.split(": ")[0] for line in solver.diagnostic_lines(report)] == ["captcha 1.1"]
    assert " step=unavailable " in solver.diagnostic_lines(report)[0]


def test_diagnostic_lines_drop_unknown_values():
    report = CaptchaReport()
    report.diagnostics.append({"seq": 1, "try": 1, "variant": "<script>", "step": "gesture_error", "angle": "x",
                               "distance": None, "requests": "2", "moves": 1, "gesture_ms": -5, "http": "504 secret",
                               "captcha_button": "?", "ms": 3})
    report.diagnostics.append({"seq": 1, "try": 2, "variant": "rotate_v2", "step": "free text SENTINEL", "angle": 1,
                               "distance": 1, "requests": 1, "moves": 1, "gesture_ms": 1, "http": 200,
                               "captcha_button": None, "ms": 1})
    # s27 : épingle amendée (champs de mesure du rotate v2 en fin de ligne, console ADR 061)
    assert solver.diagnostic_lines(report) == [
        "captcha 1.1: variant=unknown step=gesture_error angle=- distance=- requests=2 moves=1 gesture_ms=0 http=- refresh=- ms=3"
        " bar=- handle=- track=- in_w=- in_nat=- out_w=- pair=- moved=- rot_in=- rot_out=- rsrc=- rsrc_out=- k1000=-"
        " target=- fix=- sense=- rot0_in=- rot0_out=- rot0=-"
    ]


def test_to_json_ignores_the_diagnostics():
    clock = Clock()
    report = CaptchaReport()
    run(FakeCamofox("rotate_v2", clock, verdicts=[[True], [False, False]]), clock, sad=FakeSad(), report=report)
    assert report.diagnostics
    expected = [
        {"seq": c["seq"], "outcome": c["outcome"], "at": c["at"], "attempts": [dict(a) for a in c["attempts"]]}
        for c in report.captchas
    ]
    assert report.to_json() == expected
    for captcha in report.to_json():
        assert set(captcha) == {"seq", "outcome", "at", "attempts"}
        for attempt in captcha["attempts"]:
            assert set(attempt) == {"try", "type", "variant", "result", "ms"}


def test_report_is_bounded():
    report = CaptchaReport()
    for i in range(7):
        report.captchas.append({
            "seq": i + 1,
            "outcome": "unsolved",
            "at": 1,
            "attempts": [{"try": t, "type": "rotate", "variant": "v1", "result": "failed", "ms": 1} for t in (1, 2, 3, 4)],
        })
    data = report.to_json()
    assert len(data) == 5 and all(len(c["attempts"]) == 3 for c in data)


def test_attempt_duration_is_measured_and_bounded():
    clock = Clock()
    client = FakeCamofox("rotate_v1", clock)
    _, report = run(client, clock, sad=FakeSad())
    ms = report.captchas[0]["attempts"][0]["ms"]
    assert 0 < ms <= 600000


def test_solver_prints_nothing(capsys):
    clock = Clock()
    for variant in (*geo.VARIANTS, None):
        run(FakeCamofox(variant, clock, verdicts=[[True], [True], [True]]), clock, sad=FakeSad())
    run(FakeCamofox("rotate_v1", clock), clock, sad=FakeSad(error=SadCaptchaError("http")))
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_solver_module_has_no_forbidden_call():
    source = (Path(solver.__file__)).read_text(encoding="utf-8")
    for banned in ("navigate(", "location.", "reload", '"/refresh"', "delete_tab", "press(", "print(", "tiktok_login"):
        assert banned not in source, banned
    # s25 : épingle amendée (rafraîchissement relu, console ADR 059): the captcha's own button is read
    # (REFRESH_JS) and clicked by the plugin; never a navigation, a page reload nor a Camofox /refresh call.
    rest = source.replace("#captcha_refresh_button", "").replace("REFRESH_BUTTON", "").replace("REFRESH_JS", "")
    rest = rest.replace("def refresh(", "").replace("self.refresh(", "").replace("solver.refresh(", "").replace("refresh=", "")
    assert "refresh" not in rest


# --- s27 : lecture tenue et auto-calibration du rotate v2 (console ADR 061) ---------------------------------------

K_NOMINAL = 360 / (BOX_BAR["width"] - BOX_HANDLE["width"])
START = geo.drag_start(BOX_HANDLE)


def gesture_requests(client):
    return [r for r in client.requests if r.get("actions") is not None and not client.is_refresh_click(r["actions"])]


def first_try_requests(client):
    """The /mouse requests of the first gesture (up to the first one that releases the button)."""
    out = []
    for r in gesture_requests(client):
        out.append(r)
        if not r["hold"]:
            break
    return out


def line_field(line, name):
    return re.search(rf" {name}=(\S+)", line).group(1)


def test_rotate_v2_nominal_path_reads_held_and_does_not_correct():
    clock = Clock()
    client = FakeCamofox("rotate_v2", clock)
    outcome, report = run(client, clock, sad=FakeSad())
    assert outcome == "solved"
    requests = first_try_requests(client)
    assert [r["hold"] for r in requests] == [True, True, False]
    assert requests[-1]["actions"] == [{"type": "up"}]
    assert client.calls.count(("held_probe",)) == 1
    diag = report.diagnostics[0]
    assert diag["fix"] == "none" and diag["moves"] == 10 and diag["requests"] == 3
    assert diag["sense"] == "direct" and diag["moved"] == 75
    line = solver.diagnostic_lines(report)[0]
    assert line.endswith(
        " bar=340 handle=40 track=340 in_w=110 in_nat=220 out_w=190 pair=ok moved=75 rot_in=90 rot_out=- rsrc=inline"
        " rsrc_out=none k1000=1200 target=75 fix=none sense=direct rot0_in=0 rot0_out=- rot0=read"
    )


def test_rotate_v2_wrong_scale_is_corrected_once_and_solved():
    clock = Clock()
    client = FakeCamofox("rotate_v2", clock, verdicts=[])
    client.k_in = 2 * K_NOMINAL
    client.solve_when = 90
    sad = FakeSad()
    outcome, report = run(client, clock, sad=sad)
    assert outcome == "solved" and len(sad.calls) == 1
    diag = report.diagnostics[0]
    assert diag["step"] == "solved" and diag["fix"] == "moved" and diag["moves"] == 11
    target = round(90 / (2 * K_NOMINAL))
    final = first_try_requests(client)[-1]["actions"]
    assert final == [{"type": "move", "x": START[0] + target, "y": START[1]}, {"type": "wait", "ms": 300}, {"type": "up"}]
    assert sum(1 for a in final if a["type"] == "move") == 1
    line = solver.diagnostic_lines(report)[0]
    assert line_field(line, "target") == str(target) and line_field(line, "k1000") == "2400"
    assert line_field(line, "fix") == "moved"


def test_rotate_v2_same_scene_without_the_correction_fails(monkeypatch):
    monkeypatch.setattr(geo, "ROTATE_FIX_MIN_PX", 10**6)
    clock = Clock()
    client = FakeCamofox("rotate_v2", clock, verdicts=[])
    client.k_in = 2 * K_NOMINAL
    client.solve_when = 90
    outcome, report = run(client, clock, sad=FakeSad())
    assert outcome == "unsolved"
    assert [d["step"] for d in report.diagnostics] == ["not_gone"] * 3
    assert all(d["fix"] == "none" for d in report.diagnostics)


def test_rotate_v2_still_cursor_is_no_move():
    clock = Clock()
    client = FakeCamofox("rotate_v2", clock)
    client.handle_follow = 0.0
    outcome, report = run(client, clock, sad=FakeSad())
    diag = report.diagnostics[0]
    assert diag["fix"] == "no_move" and diag["moved"] == 0
    assert first_try_requests(client)[-1]["actions"] == [{"type": "up"}]
    assert " moved=0 " in solver.diagnostic_lines(report)[0]


def test_rotate_v2_sense_alternates_on_a_negative_scale():
    clock = Clock()
    client = FakeCamofox("rotate_v2", clock, verdicts=[[True], [True], [True]])
    client.k_in = -K_NOMINAL
    outcome, report = run(client, clock, sad=FakeSad())
    assert outcome == "unsolved"
    assert [d["sense"] for d in report.diagnostics] == ["direct", "complement", "direct"]
    bar, handle = BOX_BAR["width"], BOX_HANDLE["width"]
    assert [d["distance"] for d in report.diagnostics] == [
        geo.rotate_distance(bar, handle, 90),
        geo.rotate_distance(bar, handle, 360 - 90),
        geo.rotate_distance(bar, handle, 90),
    ]
    assert [line_field(line, "sense") for line in solver.diagnostic_lines(report)] == ["direct", "complement", "direct"]


def test_rotate_v2_sense_kept_without_a_read():
    clock = Clock()
    client = FakeCamofox("rotate_v2", clock, verdicts=[[True], [True], [True]])
    client.k_in = -K_NOMINAL
    client.probe_raises = CamofoxHTTPError(500)
    _, report = run(client, clock, sad=FakeSad())
    assert report.diagnostics[0]["fix"] == "no_read"
    assert report.diagnostics[1]["sense"] == "direct"


def test_rotate_v2_positive_scale_stays_direct():
    clock = Clock()
    client = FakeCamofox("rotate_v2", clock, verdicts=[[True], [True], [True]])
    _, report = run(client, clock, sad=FakeSad())
    assert [d["sense"] for d in report.diagnostics] == ["direct"] * 3


def test_rotate_v2_complement_scene_is_solved_at_the_second_try():
    clock = Clock()
    client = FakeCamofox("rotate_v2", clock, verdicts=[])
    client.k_in = -K_NOMINAL
    client.solve_when = 90
    outcome, report = run(client, clock, sad=FakeSad())
    assert outcome == "solved"
    assert [d["step"] for d in report.diagnostics] == ["not_gone", "solved"]
    lines = solver.diagnostic_lines(report)
    assert lines[1].startswith("captcha 1.2: variant=rotate_v2 step=solved ")
    assert line_field(lines[1], "sense") == "complement"


def test_rotate_v2_outer_image_turning_too():
    clock = Clock()
    client = FakeCamofox("rotate_v2", clock, verdicts=[])
    client.k_out = -K_NOMINAL
    client.rsrc_out = "inline"
    client.solve_when = 90
    outcome, report = run(client, clock, sad=FakeSad())
    assert outcome == "solved"
    diag = report.diagnostics[0]
    assert diag["fix"] == "moved" and diag["target"] == round(90 / (2 * K_NOMINAL))
    line = solver.diagnostic_lines(report)[0]
    assert line_field(line, "rot_out") == "-90" and line_field(line, "rsrc_out") == "inline"
    assert line_field(line, "rot_in") == "90"


def test_rotate_v2_rest_rotation_is_subtracted():
    # review m1: the inner image already shows 30° before the drag; the correction measures the drag's rotation
    clock = Clock()
    client = FakeCamofox("rotate_v2", clock, verdicts=[])
    client.k_in = 2 * K_NOMINAL
    client.rest_in = 30.0
    client.solve_when = 90
    outcome, report = run(client, clock, sad=FakeSad())
    assert outcome == "solved"
    diag = report.diagnostics[0]
    target = round(90 / (2 * K_NOMINAL))
    assert diag["fix"] == "moved" and diag["target"] == target
    line = solver.diagnostic_lines(report)[0]
    assert line_field(line, "k1000") == "2400" and line_field(line, "rot_in") == "210"  # 30 + 2.4 × 75
    assert line.endswith(" rot0_in=30 rot0_out=- rot0=read")


def test_rotate_v2_missing_rest_read_counts_as_zero_and_is_flagged():
    clock = Clock()
    client = FakeCamofox("rotate_v2", clock, verdicts=[])
    client.k_in = 2 * K_NOMINAL
    client.rest_in = None
    client.solve_when = 90
    outcome, report = run(client, clock, sad=FakeSad())
    assert outcome == "solved"
    assert report.diagnostics[0]["target"] == round(90 / (2 * K_NOMINAL))
    line = solver.diagnostic_lines(report)[0]
    assert line_field(line, "k1000") == "2400"
    assert line.endswith(" rot0_in=- rot0_out=- rot0=missing")


def test_rotate_v2_absurd_rotation_reads_never_raise():
    # review m2: finite but enormous readings would give k = inf and round(inf) raises
    clock = Clock()
    client = FakeCamofox("rotate_v2", clock)
    client.rsrc_out = "inline"
    client.probe_extra = {"rot_in": 1.7e308, "rot_out": -1.7e308}
    outcome, report = run(client, clock, sad=FakeSad())
    assert outcome == "solved"
    assert report.diagnostics[0]["fix"] == "no_rot"
    line = solver.diagnostic_lines(report)[0]
    assert line_field(line, "k1000") == "-" and line_field(line, "fix") == "no_rot"
    assert first_try_requests(client)[-1]["actions"] == [{"type": "up"}]


@pytest.mark.parametrize(
    "error",
    [CamofoxTimeout(), CamofoxUnreachable(), CamofoxHTTPError(500), RuntimeError("boom")],
    ids=["timeout", "unreachable", "http500", "other"],
)
def test_rotate_v2_failed_held_read_releases_at_once_and_goes_on(error):
    clock = Clock()
    client = FakeCamofox("rotate_v2", clock)
    client.probe_raises = error
    outcome, report = run(client, clock, sad=FakeSad())
    assert outcome == "solved"  # never a _Stop because of the read
    diag = report.diagnostics[0]
    assert diag["fix"] == "no_read" and diag["step"] == "solved"
    index = client.calls.index(("held_probe",))
    following = client.calls[index + 1]
    assert following[0] == "mouse" and following[2] == [{"type": "up"}] and following[3] is False
    requests = first_try_requests(client)
    assert requests[-1]["start"] - requests[-2]["end"] <= solver.HELD_PROBE_TIMEOUT_S
    assert client.button_down is False
    assert line_field(solver.diagnostic_lines(report)[0], "fix") == "no_read"


def test_rotate_v2_error_in_the_decision_still_releases(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("decision")

    monkeypatch.setattr(geo, "rotate_target", boom)
    clock = Clock()
    client = FakeCamofox("rotate_v2", clock)
    with pytest.raises(RuntimeError):
        run(client, clock, sad=FakeSad())
    requests = first_try_requests(client)
    assert requests[-1]["actions"] == [{"type": "up"}] and requests[-1]["hold"] is False
    assert client.button_down is False


def test_rotate_v2_camofox_unreachable_for_the_final_request_is_unavailable():
    clock = Clock()
    client = FakeCamofox("rotate_v2", clock)
    original = client.mouse

    def mouse(tab_id, actions, *, hold=False, timeout=25.0):
        if client.calls.count(("held_probe",)) and actions == [{"type": "up"}]:
            client.calls.append(("mouse", tab_id, actions, hold, timeout))
            raise CamofoxUnreachable()
        return original(tab_id, actions, hold=hold, timeout=timeout)

    client.mouse = mouse
    outcome, report = run(client, clock, sad=FakeSad())
    assert outcome == "unavailable"
    assert report.diagnostics[0]["step"] == "unavailable"


def _bare_solver(clock):
    return solver._Solver(
        None, "tab-1", None, clock=clock, sleep=clock.sleep, rng=random.Random(1), deadline=clock() + 240
    )


def test_rotate_v2_bounds():
    bare = _bare_solver(Clock())
    assert solver.ROTATE_V2_MAX_REQUESTS == solver.DRAG_MAX_REQUESTS == 3
    assert solver.HELD_PROBE_TIMEOUT_S == 2.5
    assert bare._answer_bound("rotate_v2") == 132.5
    # unchanged (values at e47d7ae)
    assert bare._answer_bound("rotate_v1") == 130.0
    assert bare._answer_bound("puzzle_v1") == 130.0


@pytest.mark.parametrize(("budget", "paid"), [(147.4, False), (147.6, True)])
def test_rotate_v2_paid_call_needs_147_5_seconds(budget, paid):
    clock = Clock()
    client = FakeCamofox("rotate_v2", clock, verdicts=[[True], [True], [True]])
    sad = FakeSad()
    run(client, clock, sad=sad, budget=budget)
    assert (len(sad.calls) >= 1) is paid


def test_rotate_v2_final_request_sent_even_past_the_deadline():
    clock = Clock()
    client = FakeCamofox("rotate_v2", clock)
    client.probe_cost_s = 500.0  # the deadline is crossed during the held read
    outcome, report = run(client, clock, sad=FakeSad())
    requests = first_try_requests(client)
    assert requests[-1]["actions"] == [{"type": "up"}] and requests[-1]["hold"] is False
    assert client.button_down is False
    assert report.diagnostics[0]["step"] == "budget"
    assert outcome == "unsolved"


def test_rotate_v2_probe_hygiene():
    clock = Clock()
    client = FakeCamofox("rotate_v2", clock)
    client.probe_extra = {"src": "https://p16-sign.ibyteimg.com/SENTINEL_SRC.png", "pair": "SENTINEL_SRC",
                          "rsrc_in": "https://SENTINEL_SRC"}
    _, report = run(client, clock, sad=FakeSad())
    text = "\n".join(solver.diagnostic_lines(report))
    assert "SENTINEL_SRC" not in text and "ibyteimg" not in text and "https" not in text
    assert line_field(text, "pair") == "-" and line_field(text, "rsrc") == "-"
    for line in solver.diagnostic_lines(report):
        assert LINE.fullmatch(line), line
    clock = Clock()
    same = FakeCamofox("rotate_v2", clock)
    same.same_image = True
    _, report = run(same, clock, sad=FakeSad())
    assert line_field(solver.diagnostic_lines(report)[0], "pair") == "same"


def test_other_variants_have_no_measure_fields():
    for variant in ("rotate_v1", "puzzle_v1", "shapes_v2"):
        clock = Clock()
        _, report = run(FakeCamofox(variant, clock), clock, sad=FakeSad())
        line = solver.diagnostic_lines(report)[0]
        assert line.endswith(
            " bar=- handle=- track=- in_w=- in_nat=- out_w=- pair=- moved=- rot_in=- rot_out=- rsrc=- rsrc_out=-"
            " k1000=- target=- fix=- sense=- rot0_in=- rot0_out=- rot0=-"
        ), line
