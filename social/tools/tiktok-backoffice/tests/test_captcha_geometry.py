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


# --- s27 : lecture tenue et auto-calibration du rotate v2 (console ADR 061) ---------------------------------------

# drag_actions((10, 10), 170, random.Random(42)) captured at e47d7ae, before the gesture was cut in two (P2, D4).
DRAG_ACTIONS_E47D7AE = [
    {"type": "move", "x": 10.0, "y": 10.0}, {"type": "wait", "ms": 744}, {"type": "down"},
    {"type": "move", "x": 39.16666666666667, "y": 10.0}, {"type": "wait", "ms": 10},
    {"type": "move", "x": 68.33333333333334, "y": 10.0}, {"type": "wait", "ms": 10},
    {"type": "move", "x": 97.5, "y": 10.0}, {"type": "wait", "ms": 10},
    {"type": "move", "x": 126.66666666666667, "y": 10.0}, {"type": "wait", "ms": 10},
    {"type": "move", "x": 155.83333333333334, "y": 10.0}, {"type": "wait", "ms": 10},
    {"type": "move", "x": 185.0, "y": 10.0}, {"type": "wait", "ms": 10}, {"type": "wait", "ms": 250},
    {"type": "move", "x": 183.33333333333334, "y": 11.0}, {"type": "wait", "ms": 5},
    {"type": "move", "x": 181.66666666666666, "y": 9.0}, {"type": "wait", "ms": 5}, {"type": "wait", "ms": 200},
    {"type": "move", "x": 180.0, "y": 10.0}, {"type": "wait", "ms": 300}, {"type": "up"},
]


def test_drag_actions_is_the_e47d7ae_literal():
    assert geo.drag_actions((10, 10), 170, random.Random(42)) == DRAG_ACTIONS_E47D7AE


@pytest.mark.parametrize("offset", OFFSETS)
def test_drag_actions_is_the_held_part_then_the_release(offset):
    start = (120.5, 300.25)
    for seed in SEEDS:
        held = geo.drag_actions_held(start, offset, random.Random(seed))
        assert geo.drag_actions(start, offset, random.Random(seed)) == held + geo.finish_actions(start, None)
        assert held[-1] == {"type": "wait", "ms": 300}  # the wait after the final move is kept before the read
        assert [a["type"] for a in held].count("up") == 0


def test_finish_actions():
    start = (120.5, 300.25)
    assert geo.finish_actions(start, None) == [{"type": "up"}]
    assert geo.finish_actions(start, 12) == [
        {"type": "move", "x": 132.5, "y": 300.25},
        {"type": "wait", "ms": 300},
        {"type": "up"},
    ]


@pytest.mark.parametrize("offset", OFFSETS)
def test_held_drag_fits_in_two_held_requests(offset):
    from app.captcha_solver import DRAG_MAX_REQUESTS

    for seed in SEEDS:
        chunks = geo.split_gesture(geo.drag_actions_held((120.5, 300.25), offset, random.Random(seed)))
        assert 1 <= len(chunks) <= DRAG_MAX_REQUESTS - 1
        assert all(hold is True for _, hold in chunks)
        for chunk, _ in chunks:
            assert geo.request_worst_case_s(chunk) <= geo.GESTURE_REQUEST_BUDGET_S


def test_held_gap_stays_under_the_plugin_release():
    from app.captcha_solver import HELD_PROBE_TIMEOUT_S

    final = geo.finish_actions((120.5, 300.25), 12)
    assert geo.request_worst_case_s(final) <= geo.GESTURE_REQUEST_BUDGET_S
    assert 2 * HELD_PROBE_TIMEOUT_S + geo.request_worst_case_s(final) < geo.PLUGIN_HOLD_RELEASE_S


RUN922 = _load(FIXTURES / "run922_rotate_v2.json")
TRAVEL = RUN922["geometry"]["bar_width"] - RUN922["geometry"]["handle_width"]
K_NOMINAL = 360 / TRAVEL


def _fix(angle, *, moved=None, k=K_NOMINAL, rot_in=None, rot_out=None, rsrc_in="inline", rsrc_out="none",
         sense="direct", distance=None, travel=TRAVEL):
    wanted = angle if sense == "direct" else 360 - angle
    if distance is None:
        distance = geo.rotate_distance(RUN922["geometry"]["bar_width"], RUN922["geometry"]["handle_width"], wanted)
    if moved is None:
        moved = distance
    if rot_in is None and k is not None and moved is not None:
        rot_in = k * moved
    return geo.rotate_target(angle, travel, distance, moved, rot_in, rot_out, rsrc_in, rsrc_out, sense), distance


def test_run922_fixture_and_the_story_angles():
    assert TRAVEL == 284
    bar, handle = RUN922["geometry"]["bar_width"], RUN922["geometry"]["handle_width"]
    for observed in RUN922["observed"]:
        assert geo.rotate_distance(bar, handle, observed["angle"]) == observed["distance"]
    assert [o["angle"] for o in RUN922["observed"]] == [166, 148, 99]
    assert [o["distance"] for o in RUN922["observed"]] == [130, 116, 78]
    assert geo.rotate_distance(bar, handle, 0) == 0
    assert geo.rotate_distance(bar, handle, 359) == 283
    assert geo.rotate_distance(bar, handle, 360) == 284


@pytest.mark.parametrize("angle", [166, 148, 99])
def test_rotate_target_at_the_nominal_scale_keeps_the_estimate(angle):
    fix, distance = _fix(angle)
    assert fix.code == "none"
    assert fix.cursor == distance


def test_rotate_target_double_scale_halves_the_target():
    # the outer image turns the other way: the relative rotation is twice the inner one
    distance = geo.rotate_distance(348, 64, 99)
    rot_in = K_NOMINAL * distance
    fix, _ = _fix(99, rot_in=rot_in, rot_out=-rot_in, rsrc_out="inline")
    assert fix.code == "moved"
    assert fix.k == pytest.approx(2 * K_NOMINAL)
    assert fix.target == round(99 / (2 * K_NOMINAL))
    assert abs(fix.target - distance / 2) <= 1
    assert fix.cursor == distance + (fix.target - distance)


def test_rotate_target_half_scale_is_bounded_to_the_travel():
    fix, distance = _fix(200, k=K_NOMINAL / 2)
    assert round(200 / (K_NOMINAL / 2)) == 316
    assert fix.code == "moved" and fix.target == 284
    assert fix.cursor == distance + (284 - distance)


def test_rotate_target_half_scale_166():
    fix, distance = _fix(166, k=K_NOMINAL / 2)
    assert fix.code == "moved" and fix.target == 262
    assert fix.cursor == distance + (262 - distance)


def test_rotate_target_compensates_a_lagging_cursor():
    distance = geo.rotate_distance(348, 64, 166)
    fix, _ = _fix(166, moved=distance - 8)
    assert fix.code == "moved"
    assert fix.cursor == distance + (fix.target - (distance - 8))


def test_rotate_target_still_cursor():
    fix, _ = _fix(166, moved=3, distance=130)
    assert fix.code == "no_move"
    assert fix.cursor == 130
    short, _ = _fix(15, moved=3, distance=12)
    assert 12 < 2 * geo.ROTATE_MIN_MOVED_PX
    assert short.code == "none" and short.cursor == 12


def test_rotate_target_without_a_read():
    fix, distance = _fix(166, rot_in=None, k=None)
    assert fix.code == "no_rot" and fix.cursor == distance
    no_read = geo.rotate_target(166, TRAVEL, 130, None, None, None, None, None, "direct")
    assert no_read.code == "no_read" and no_read.cursor == 130


def test_rotate_target_implausible_scale():
    fix, distance = _fix(99, k=5 * K_NOMINAL)
    assert fix.code == "implausible" and fix.cursor == distance


def test_rotate_target_ambiguity_is_judged_per_image_on_the_raw_read():
    assert _fix(170, rot_in=170, rsrc_in="matrix")[0].code == "ambiguous"
    assert _fix(210, rot_in=210, rsrc_in="inline")[0].code in ("none", "moved")
    distance = geo.rotate_distance(348, 64, 100)
    assert _fix(100, rot_in=100, rsrc_in="inline", rot_out=-160, rsrc_out="matrix", moved=distance)[0].code == "ambiguous"
    assert _fix(120, rot_in=120, rsrc_in="parent")[0].code in ("none", "moved")
    assert _fix(160, rot_in=160, rsrc_in="parent")[0].code == "ambiguous"


def test_rotate_target_negative_rotation_has_the_same_target():
    positive, _ = _fix(99, k=2 * K_NOMINAL)
    negative, _ = _fix(99, k=-2 * K_NOMINAL)
    assert negative.target == positive.target and negative.cursor == positive.cursor
    assert negative.k == pytest.approx(-2 * K_NOMINAL)


def test_rotate_target_complement_sense():
    assert geo.rotate_wanted(166, "complement") == 194
    assert geo.rotate_wanted(166, "direct") == 166
    fix, distance = _fix(166, sense="complement", k=2 * K_NOMINAL)
    assert distance == geo.rotate_distance(348, 64, 194)
    assert fix.target == round(194 / (2 * K_NOMINAL))


def test_rotate_target_small_cases():
    fix = geo.rotate_target(0, TRAVEL, 0, 0, 0, None, "inline", "none", "direct")
    assert fix.code == "none" and fix.cursor == 0
    # |target − moved| = 1 ⇒ no correction
    one = geo.rotate_target(166, TRAVEL, 130, 130, K_NOMINAL * 130, None, "inline", "none", "direct")
    assert abs(one.target - 130) == 1 and one.code == "none" and one.cursor == 130
    # the cursor is never negative
    back = geo.rotate_target(0, TRAVEL, 0, 40, K_NOMINAL * 40, None, "inline", "none", "direct")
    assert back.code == "moved" and back.cursor == 0


BAD = (None, float("nan"), float("inf"), float("-inf"), "x", True)


@pytest.mark.parametrize("position", range(9))
def test_rotate_target_never_raises(position):
    good = [166, TRAVEL, 130, 130, K_NOMINAL * 130, 0.0, "inline", "inline", "direct"]
    for bad in BAD:
        args = list(good)
        args[position] = bad
        fix = geo.rotate_target(*args)
        assert fix.code in geo.FIX_CODES
        assert isinstance(fix.cursor, int) and fix.cursor >= 0


def test_rotate_target_subtracts_the_rest_rotation():
    # review m1: the inner image already shows 30° before the drag; only the drag's rotation measures k
    distance = geo.rotate_distance(348, 64, 99)
    rest = geo.rotate_target(99, TRAVEL, distance, distance, 30 + 2 * K_NOMINAL * distance, None, "inline", "none",
                             "direct", rot0_in=30)
    assert rest.code == "moved" and rest.k == pytest.approx(2 * K_NOMINAL)
    assert rest.target == round(99 / (2 * K_NOMINAL))
    nominal = geo.rotate_target(99, TRAVEL, distance, distance, 30 + K_NOMINAL * distance, None, "inline", "none",
                                "direct", rot0_in=30)
    assert nominal.code == "none" and nominal.cursor == distance
    # the outer image's rest rotation is subtracted the same way
    outer = geo.rotate_target(99, TRAVEL, distance, distance, K_NOMINAL * distance, -20 - K_NOMINAL * distance,
                              "inline", "inline", "direct", rot0_out=-20)
    assert outer.k == pytest.approx(2 * K_NOMINAL) and outer.target == rest.target


@pytest.mark.parametrize("missing", [None, float("nan"), float("inf"), "x", True])
def test_rotate_target_missing_rest_rotation_is_zero(missing):
    distance = geo.rotate_distance(348, 64, 99)
    args = (99, TRAVEL, distance, distance, 2 * K_NOMINAL * distance, None, "inline", "none", "direct")
    assert geo.rotate_target(*args, rot0_in=missing, rot0_out=missing) == geo.rotate_target(*args)


def test_rotate_rest_state():
    assert geo.rotate_rest_state(90, None, 0, None) == "read"
    assert geo.rotate_rest_state(90, -90, 0, 5) == "read"
    assert geo.rotate_rest_state(90, None, None, None) == "missing"
    assert geo.rotate_rest_state(90, -90, 0, None) == "missing"
    assert geo.rotate_rest_state(None, None, None, None) == "read"  # nothing held to subtract from
    assert geo.REST_STATES == ("read", "missing")


@pytest.mark.parametrize(
    "rot_in, rot_out, rot0_in, rot0_out",
    [
        (1.7e308, -1.7e308, 0.0, 0.0),
        (1e6, None, 0.0, 0.0),
        (90.0, 1e6, 0.0, 0.0),
        (90.0, None, 1e6, 0.0),
        (90.0, None, 0.0, -1e6),
    ],
)
def test_rotate_target_absurd_readings_are_unreadable(rot_in, rot_out, rot0_in, rot0_out):
    # review m2: never an infinite k (round(inf) raises), an absurd reading is no reading
    fix = geo.rotate_target(166, TRAVEL, 130, 130, rot_in, rot_out, "inline", "inline", "direct",
                            rot0_in=rot0_in, rot0_out=rot0_out)
    assert fix.code == "no_rot" and fix.k is None and fix.cursor == 130


def test_closed_lists():
    assert geo.FIX_CODES == ("none", "moved", "no_read", "no_rot", "no_move", "implausible", "ambiguous")
    assert geo.SENSES == ("direct", "complement")
    assert geo.ROTATE_SOURCES == ("inline", "matrix", "parent", "none")
    assert (geo.ROTATE_MIN_MOVED_PX, geo.ROTATE_FIX_MIN_PX, geo.ROTATE_AMBIGUOUS_DEG) == (10, 2, 150)
    assert (geo.ROTATE_K_MIN_RATIO, geo.ROTATE_K_MAX_RATIO) == (0.25, 4)
