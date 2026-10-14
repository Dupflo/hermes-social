"""s22 — app/captcha_geometry.py: identification and response → gesture conversion, pure (console ADR 053).

Synthetic fixtures written for the story (tests/fixtures/captcha): no upstream file is copied.
"""

from __future__ import annotations

import json
import math
import random
import re
from pathlib import Path

import pytest

from app import captcha_geometry as geo

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "captcha"
FACTS = sorted((FIXTURES / "facts").glob("*.json"))
RESPONSES = sorted((FIXTURES / "responses").glob("*.json"))
EIGHT = {"puzzle_v1", "puzzle_v2", "rotate_v1", "rotate_v2", "shapes_v1", "shapes_v2", "icon_v1", "icon_v2"}


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_fixtures_cover_the_eight_variants_and_none():
    names = {p.stem for p in FACTS}
    assert EIGHT | {"none", "shapes_vs_icon_by_src"} <= names
    assert set(geo.VARIANTS) == EIGHT


@pytest.mark.parametrize("path", FACTS, ids=lambda p: p.stem)
def test_identify_on_each_fixture(path):
    fixture = _load(path)
    assert geo.identify(fixture["facts"]) == fixture["expected"]


@pytest.mark.parametrize("facts", [None, [], "x", {}, {"present": True, "puzzle_v1": "true"}])
def test_identify_rejects_garbage(facts):
    assert geo.identify(facts) is None


def test_variant_parts():
    assert geo.variant_parts("puzzle_v2") == ("puzzle", "v2")
    assert geo.variant_parts("icon_v1") == ("icon", "v1")
    with pytest.raises(ValueError):
        geo.variant_parts("nope")


def test_story_formulas():
    assert geo.puzzle_distance(0.5, 340) == 170
    assert geo.rotate_distance(340, 64, 90) == 69


@pytest.mark.parametrize("path", RESPONSES, ids=lambda p: p.stem)
def test_response_fixture_converts(path):
    fixture = _load(path)
    kind, geometry, expected = fixture["kind"], fixture["geometry"], fixture["expected"]
    value = geo.validate_response(kind, fixture["response"])
    if kind == "rotate":
        assert geo.rotate_distance(geometry["bar_width"], geometry["handle_width"], value) == expected["distance"]
    elif kind == "puzzle":
        assert geo.puzzle_distance(value, geometry["image_width"]) == expected["distance"]
    else:
        points = value if kind == "icon" else list(value)
        got = [geo.proportional_point(geometry["box"], px, py) for px, py in points]
        assert len(got) == len(expected["points"])
        for (x, y), (ex, ey) in zip(got, expected["points"]):
            assert x == pytest.approx(ex) and y == pytest.approx(ey)


def test_proportional_point():
    assert geo.proportional_point({"x": 10, "y": 20, "width": 100, "height": 50}, 0.5, 1) == (60, 70)


def _waits(actions):
    return sum(a["ms"] for a in actions if a["type"] == "wait")


@pytest.mark.parametrize("offset", [0, 1, 37, 69, 170, 250, 333, 400])
def test_drag_actions_shape(offset):
    start = (120.5, 300.25)
    actions = geo.drag_actions(start, offset, random.Random(7))
    types = [a["type"] for a in actions]
    assert types.count("down") == 1
    assert types.count("up") == 1
    assert types[-1] == "up"
    assert types.index("down") < types.index("up")
    moves = [a for a in actions if a["type"] == "move"]
    assert moves[0]["x"] == start[0] and moves[0]["y"] == start[1]
    assert moves[-1]["x"] == start[0] + offset and moves[-1]["y"] == start[1]
    # s25 : épingle amendée (geste sous humanize, console ADR 059)
    assert all("steps" not in m for m in moves)
    assert len(moves) <= 10
    assert len(actions) <= 400
    # advance = the moves after the start one, rising in x up to the overshoot point
    peak = max(range(len(moves)), key=lambda i: moves[i]["x"])
    advance = moves[1 : peak + 1]
    assert all(b["x"] > a["x"] for a, b in zip(advance, advance[1:]))
    assert len(advance) <= geo.DRAG_ADVANCE_MOVES
    for move in moves:
        assert move["x"] >= 0 and move["y"] >= 0
        assert math.isfinite(move["x"]) and math.isfinite(move["y"])
    assert _waits(actions) <= 15000
    for action in actions:
        if action["type"] == "wait":
            assert isinstance(action["ms"], int) and 0 <= action["ms"] <= 2000
    # the drag goes past the target (+5) before coming back
    assert max(m["x"] for m in moves) >= start[0] + offset + 1 or offset == 0


OFFSETS = [0, 1, 5, 37, 69, 120, 170, 250, 333, 400]
SEEDS = range(20)


def _button_states(chunks):
    """Button state at the end of each request, replaying down/up across requests."""
    down, states = False, []
    for chunk, _hold in chunks:
        for action in chunk:
            if action["type"] == "down":
                down = True
            elif action["type"] == "up":
                down = False
        states.append(down)
    return states


def _check_split(actions, max_requests):
    chunks = geo.split_gesture(actions)
    assert 1 <= len(chunks) <= max_requests
    assert [a for chunk, _ in chunks for a in chunk] == actions
    assert all(chunk for chunk, _ in chunks)
    for chunk, _ in chunks:
        assert geo.request_worst_case_s(chunk) <= geo.GESTURE_REQUEST_BUDGET_S
    states = _button_states(chunks)
    assert [hold for _, hold in chunks] == states
    assert chunks[-1][1] is False
    return chunks


@pytest.mark.parametrize("offset", OFFSETS)
def test_split_drag_keeps_every_request_under_budget(offset):
    for seed in SEEDS:
        actions = geo.drag_actions((120.5, 300.25), offset, random.Random(seed))
        chunks = _check_split(actions, 3)
        assert chunks[-1][0][-1]["type"] == "up"
        assert len(chunks) >= 2  # worst case of the whole drag is above one request's budget


@pytest.mark.parametrize("n", range(1, geo.MAX_ICON_POINTS + 1))
def test_split_clicks_cut_after_an_up(n):
    for seed in SEEDS:
        points = [(10.0 + i, 20.0 + i) for i in range(n)]
        actions = geo.click_actions(points, random.Random(seed))
        chunks = _check_split(actions, math.ceil(n / 3))
        for chunk, hold in chunks[:-1]:
            assert chunk[-1]["type"] == "up"
            assert hold is False


def test_split_gesture_refuses_an_action_above_the_budget():
    with pytest.raises(ValueError):
        geo.split_gesture([{"type": "move", "x": 1, "y": 1, "steps": 20}, {"type": "up"}])


def test_request_worst_case_model():
    actions = [
        {"type": "move", "x": 1, "y": 1},
        {"type": "wait", "ms": 500},
        {"type": "down"},
        {"type": "up"},
    ]
    assert geo.request_worst_case_s(actions) == pytest.approx(
        geo.HUMANIZE_MOVE_MAX_S + 0.5 + 2 * geo.BUTTON_ACTION_S
    )


PLUGIN_SOURCE = (
    Path(__file__).resolve().parents[4] / "deploy" / "camofox-plugins" / "console-mouse" / "index.js"
).read_text(encoding="utf-8")


def _plugin_limit(name):
    match = re.search(rf"^\s*{name}:\s*(\d+),", PLUGIN_SOURCE, re.M)
    assert match, name
    return int(match.group(1))


def test_cost_model_mirrors_the_plugin_limits():
    assert geo.GESTURE_REQUEST_BUDGET_S * 2 <= geo.PLUGIN_MAX_RUNTIME_S
    assert geo.PLUGIN_MAX_RUNTIME_S * 1000 == _plugin_limit("maxRuntimeMs")
    assert geo.PLUGIN_HOLD_RELEASE_S * 1000 == _plugin_limit("holdReleaseMs")


def _pre_s25_drag(sx, sy, offset):
    """The pre-s25 rotate gesture, rebuilt literally (≈ 85 moves + a final move at steps: 75)."""
    step = max(1, math.ceil(offset / 120))
    actions = [{"type": "move", "x": sx, "y": sy}, {"type": "wait", "ms": 500}, {"type": "down"}]
    for pixel in range(0, offset + 5, step):
        actions += [{"type": "move", "x": sx + pixel, "y": sy}, {"type": "wait", "ms": 10}]
    actions.append({"type": "wait", "ms": 250})
    for pixel in range(-5, 2):
        actions += [{"type": "move", "x": sx + offset - pixel, "y": max(0, sy + pixel)}, {"type": "wait", "ms": 5}]
    actions += [
        {"type": "wait", "ms": 200},
        {"type": "move", "x": sx + offset, "y": sy, "steps": 75},
        {"type": "wait", "ms": 300},
        {"type": "up"},
    ]
    return actions


def test_pre_s25_gesture_is_classified_above_the_plugin_ceiling():
    old = _pre_s25_drag(120.0, 300.0, 78)
    assert sum(1 for a in old if a["type"] == "move") >= 85
    assert geo.request_worst_case_s(old) > geo.PLUGIN_MAX_RUNTIME_S
    new = geo.drag_actions((120.0, 300.0), 78, random.Random(1))
    assert geo.request_worst_case_s(new) < geo.request_worst_case_s(old)


def test_drag_actions_deterministic_with_a_fixed_seed():
    a = geo.drag_actions((10, 10), 170, random.Random(42))
    b = geo.drag_actions((10, 10), 170, random.Random(42))
    assert a == b


def test_drag_actions_near_the_top_edge_never_goes_negative():
    actions = geo.drag_actions((10, 2), 100, random.Random(1))
    assert all(a["y"] >= 0 for a in actions if a["type"] == "move")


def test_drag_start_is_inside_the_handle():
    x, y = geo.drag_start({"x": 100, "y": 50, "width": 40, "height": 40})
    assert 100 < x < 140 and 50 < y < 90
    assert x == pytest.approx(100 + 40 / 1.337)


def test_click_actions_one_down_up_pair_per_point():
    points = [(10.0, 20.0), (30.5, 40.0), (50.0, 60.0)]
    actions = geo.click_actions(points, random.Random(3))
    downs = [i for i, a in enumerate(actions) if a["type"] == "down"]
    ups = [i for i, a in enumerate(actions) if a["type"] == "up"]
    assert len(downs) == len(ups) == len(points)
    for d, u in zip(downs, ups):
        assert d < u
    moves = [(a["x"], a["y"]) for a in actions if a["type"] == "move"]
    assert moves == points
    # ten points stay under the plugin's wait budget
    ten = geo.click_actions([(1.0, 1.0)] * 10, random.Random(9))
    assert _waits(ten) <= 15000


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("puzzle", {"slideXProportion": float("nan")}),
        ("puzzle", {"slideXProportion": 1.01}),
        ("puzzle", {"slideXProportion": -0.1}),
        ("puzzle", {"slideXProportion": True}),
        ("puzzle", {}),
        ("rotate", {"angle": 361}),
        ("rotate", {"angle": "90"}),
        ("rotate", {"angle": float("inf")}),
        ("shapes", {"pointOneProportionX": 0.1, "pointOneProportionY": 0.1, "pointTwoProportionX": 0.1}),
        ("shapes", {"pointOneProportionX": 2, "pointOneProportionY": 0.1, "pointTwoProportionX": 0.1, "pointTwoProportionY": 0}),
        ("icon", {"proportionalPoints": []}),
        ("icon", {"proportionalPoints": [{"proportionX": 0.1, "proportionY": 0.1}] * 11}),
        ("icon", {"proportionalPoints": [{"proportionX": 0.1}]}),
        ("icon", {"proportionalPoints": "x"}),
        ("unknown", {"angle": 1}),
        ("rotate", ["angle"]),
    ],
)
def test_validate_response_refuses(kind, payload):
    with pytest.raises(ValueError) as info:
        geo.validate_response(kind, payload)
    message = str(info.value)
    assert "nan" not in message.lower() and "1.01" not in message and "361" not in message


def test_validate_response_accepts_bounds():
    assert geo.validate_response("rotate", {"angle": 360}) == 360
    assert geo.validate_response("rotate", {"angle": 12.7}) == 12
    assert geo.validate_response("puzzle", {"slideXProportion": 0}) == 0.0
    assert geo.validate_response("icon", {"proportionalPoints": [{"proportionX": 1, "proportionY": 1}] * 10}) == [(1.0, 1.0)] * 10


@pytest.mark.parametrize(
    ("box", "ok"),
    [
        ({"x": 0, "y": 0, "width": 10, "height": 10}, True),
        ({"x": -5, "y": 3.5, "width": 1, "height": 1}, True),
        ({"x": 0, "y": 0, "width": 0, "height": 10}, False),
        ({"x": 0, "y": 0, "width": 10, "height": -1}, False),
        ({"x": float("nan"), "y": 0, "width": 10, "height": 10}, False),
        ({"x": 0, "y": float("inf"), "width": 10, "height": 10}, False),
        ({"x": 0, "y": 0, "width": 10}, False),
        ({"x": "0", "y": 0, "width": 10, "height": 10}, False),
        (None, False),
    ],
)
def test_valid_box(box, ok):
    assert geo.valid_box(box) is ok
