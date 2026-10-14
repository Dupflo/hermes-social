"""TikTok captcha: identification and response → gesture conversion, pure (s22, console ADR 053).

No network, no page: the page scripts (app/captcha_selectors.py) only return facts and rectangles, this
module decides. The formulas are the ones of the official client (rewritten, its repository has no
licence): puzzle = int(proportion × displayed image width), rotate = int((bar − handle) × angle / 360).

Gestures are lists of console-mouse actions (move/down/up/wait) bounded for the plugin: ≤ 1000 actions,
waits ≤ 2000 ms each and ≤ 15 000 ms in total, coordinates ≥ 0.

s25 : geste réduit et découpé sous humanize, console ADR 059. Camoufox runs with humanize: true, so every
move is a humanised cursor movement (up to ~1.5 s each) and the plugin cuts a request at 20 s (504). A drag
is therefore at most 10 moves without any `steps`, and split_gesture() cuts a gesture into requests whose
worst case stays under GESTURE_REQUEST_BUDGET_S, the button held between requests (plugin `hold`).
"""

from __future__ import annotations

import math
import random
from typing import Any, Mapping, Sequence

VARIANTS = (
    "puzzle_v1",
    "puzzle_v2",
    "rotate_v1",
    "rotate_v2",
    "shapes_v1",
    "shapes_v2",
    "icon_v1",
    "icon_v2",
)

# Drag pattern (P7; s25 : geste réduit et découpé sous humanize, console ADR 059): start inside the handle,
# advance in DRAG_ADVANCE_MOVES moves to 5 px past the target, come back in DRAG_RETURN_MOVES moves, then one
# exact final move, never with `steps` (humanize already draws the trajectory of each move).
DRAG_ADVANCE_MOVES = 6
DRAG_RETURN_MOVES = 2
DRAG_OVERSHOOT_PX = 5

# Cost model of one console-mouse request under humanize (console ADR 059, P1). HUMANIZE_MOVE_MAX_S is the
# Camoufox `humanize` default ("the cursor typically takes up to 1.5 seconds to move across the window"):
# an assumption, not a measure. The PLUGIN_* constants mirror deploy/camofox-plugins/console-mouse/index.js.
HUMANIZE_MOVE_MAX_S = 1.5
BUTTON_ACTION_S = 0.1
PLUGIN_MAX_RUNTIME_S = 20.0
PLUGIN_HOLD_RELEASE_S = 10.0
GESTURE_REQUEST_BUDGET_S = 10.0
CLICK_PAUSE_MS = (90, 600)
MAX_ICON_POINTS = 10


def _true(facts: Mapping[str, Any], key: str) -> bool:
    return facts.get(key) is True


def identify(facts: Any) -> str | None:
    """Variant name from the page facts, in the upstream order; None when nothing is recognised."""
    if not isinstance(facts, dict):
        return None
    for variant in ("puzzle_v1", "puzzle_v2", "rotate_v1", "rotate_v2"):
        if _true(facts, variant):
            return variant
    kind = facts.get("img_src_kind")
    for version in ("v1", "v2"):
        if _true(facts, f"submit_{version}"):
            # Same submit button for shapes and icon: the image src decides; neither ⇒ shapes (upstream).
            return f"icon_{version}" if kind == "icon" else f"shapes_{version}"
    return None


def variant_parts(variant: str) -> tuple[str, str]:
    if variant not in VARIANTS:
        raise ValueError("unknown captcha variant")
    kind, version = variant.split("_")
    return kind, version


def puzzle_distance(proportion: float, image_width: float) -> int:
    return int(proportion * image_width)


def rotate_distance(bar_width: float, handle_width: float, angle: float) -> int:
    return int(((bar_width - handle_width) * angle) / 360)


def proportional_point(box: Mapping[str, float], px: float, py: float) -> tuple[float, float]:
    return (box["x"] + px * box["width"], box["y"] + py * box["height"])


def drag_start(box: Mapping[str, float]) -> tuple[float, float]:
    return (box["x"] + box["width"] / 1.337, box["y"] + box["height"] / 1.337)


def _move(x: float, y: float, steps: int | None = None) -> dict[str, Any]:
    action: dict[str, Any] = {"type": "move", "x": max(0.0, float(x)), "y": max(0.0, float(y))}
    if steps is not None:
        action["steps"] = steps
    return action


def _wait(ms: int) -> dict[str, Any]:
    return {"type": "wait", "ms": int(ms)}


def drag_actions(start: tuple[float, float], offset: int, rng: random.Random) -> list[dict[str, Any]]:
    sx, sy = float(start[0]), float(start[1])
    offset = max(0, int(offset))
    far = offset + DRAG_OVERSHOOT_PX
    actions = [_move(sx, sy), _wait(rng.randint(90, 900)), {"type": "down"}]
    for index in range(1, DRAG_ADVANCE_MOVES + 1):
        actions.append(_move(sx + far * index / DRAG_ADVANCE_MOVES, sy))
        actions.append(_wait(10))
    actions.append(_wait(250))
    for index in range(1, DRAG_RETURN_MOVES + 1):
        back = DRAG_OVERSHOOT_PX * (DRAG_RETURN_MOVES + 1 - index) / (DRAG_RETURN_MOVES + 1)
        actions.append(_move(sx + offset + back, sy + (1 if index % 2 else -1)))
        actions.append(_wait(5))
    actions.append(_wait(200))
    actions.append(_move(sx + offset, sy))
    actions.append(_wait(300))
    actions.append({"type": "up"})
    return actions


def request_worst_case_s(actions: Sequence[Mapping[str, Any]]) -> float:
    """Worst-case duration of one console-mouse request under humanize (console ADR 059, P1).

    A move with `steps` is played by Playwright as that many humanised moves, hence counted `steps` times.
    """
    total = 0.0
    for action in actions:
        kind = action.get("type")
        if kind == "move":
            total += max(1, int(action.get("steps") or 1)) * HUMANIZE_MOVE_MAX_S
        elif kind == "wait":
            total += int(action.get("ms", 0)) / 1000
        elif kind in ("down", "up"):
            total += BUTTON_ACTION_S
    return total


def split_gesture(actions: Sequence[Mapping[str, Any]]) -> list[tuple[list[dict[str, Any]], bool]]:
    """Greedy split of a gesture into plugin requests, each ≤ GESTURE_REQUEST_BUDGET_S in the worst case.

    Order kept; a cut goes right after an `up` when the current request has one (never in the middle of a
    click); `hold` is True exactly when the button is still down at the end of the request (the last one ends
    with the button released). ValueError when a single action is above the budget.
    """
    chunks: list[tuple[list[dict[str, Any]], bool]] = []
    current: list[dict[str, Any]] = []
    down = False  # button state at the start of `current`

    def close(chunk: list[dict[str, Any]], start_down: bool) -> bool:
        state = start_down
        for action in chunk:
            if action["type"] == "down":
                state = True
            elif action["type"] == "up":
                state = False
        chunks.append((chunk, state))
        return state

    for action in actions:
        action = dict(action)
        if request_worst_case_s([action]) > GESTURE_REQUEST_BUDGET_S:
            raise ValueError("gesture action above the request budget")
        if current and request_worst_case_s(current + [action]) > GESTURE_REQUEST_BUDGET_S:
            ups = [i for i, a in enumerate(current) if a["type"] == "up"]
            cut = ups[-1] + 1 if ups else len(current)
            head, rest = current[:cut], current[cut:]
            down = close(head, down)
            current = rest
        current.append(action)
    if current:
        close(current, down)
    return chunks


def click_actions(points: Sequence[tuple[float, float]], rng: random.Random) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for x, y in points:
        actions.append(_move(x, y))
        actions.append(_wait(rng.randint(*CLICK_PAUSE_MS)))
        actions.append({"type": "down"})
        actions.append(_wait(1))
        actions.append({"type": "up"})
        actions.append(_wait(rng.randint(*CLICK_PAUSE_MS)))
    return actions


def _number(value: Any, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid sadcaptcha response")
    if not math.isfinite(value) or value < low or value > high:
        raise ValueError("invalid sadcaptcha response")
    return float(value)


def validate_response(kind: str, payload: Any) -> Any:
    """Typed value of a SadCaptcha answer; ValueError (without any value in the message) otherwise."""
    if not isinstance(payload, dict):
        raise ValueError("invalid sadcaptcha response")
    if kind == "rotate":
        return int(_number(payload.get("angle"), 0, 360))
    if kind == "puzzle":
        return _number(payload.get("slideXProportion"), 0, 1)
    if kind == "shapes":
        return (
            (_number(payload.get("pointOneProportionX"), 0, 1), _number(payload.get("pointOneProportionY"), 0, 1)),
            (_number(payload.get("pointTwoProportionX"), 0, 1), _number(payload.get("pointTwoProportionY"), 0, 1)),
        )
    if kind == "icon":
        points = payload.get("proportionalPoints")
        if not isinstance(points, list) or not 1 <= len(points) <= MAX_ICON_POINTS:
            raise ValueError("invalid sadcaptcha response")
        result = []
        for point in points:
            if not isinstance(point, dict):
                raise ValueError("invalid sadcaptcha response")
            result.append((_number(point.get("proportionX"), 0, 1), _number(point.get("proportionY"), 0, 1)))
        return result
    raise ValueError("invalid sadcaptcha response")


def valid_box(box: Any) -> bool:
    if not isinstance(box, dict):
        return False
    for key in ("x", "y", "width", "height"):
        value = box.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return False
    return box["width"] > 0 and box["height"] > 0
