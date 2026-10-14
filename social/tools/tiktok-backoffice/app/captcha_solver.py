"""TikTok captcha solver: SadCaptcha + trusted gestures through the console-mouse plugin (s22, console ADR 052/053).

`solve_captcha` is public and knows nothing about the login: it takes a Camofox client, a tab, a key, a report
and a deadline, so the scan (s23) can call it as is. One call = one captcha = one entry in the report:

1. the run already met a SadCaptcha error  => `service_error`, no call at all;
2. no key (vault nor deploy/.env)           => `disabled`, no mouse nor SadCaptcha call;
3. the plugin probe fails                   => `unavailable`, no SadCaptcha call;
4. at most MAX_TRIES tries, each only with ≥ MIN_TRY_BUDGET_S left before the deadline. A try = facts →
   images → one SadCaptcha call → geometry read right before the gesture → gesture → check that the captcha
   is gone. Outcomes: solved | unsolved | unrecognized | service_error | unavailable.

s25 (console ADR 059):
- rhythm: identification reads the facts for up to FACTS_WINDOW_S (a captcha being redrawn shows its
  envelope before its variant); between two rotate v2 tries the captcha's own button is located again
  (REFRESH_JS, up to REFRESH_LOOKUP_S), clicked by the plugin at the centre of the rectangle just read, then
  PAUSE_AFTER_REFRESH_S in every case; other variants keep PAUSE_BETWEEN_TRIES_S;
- split gesture: a drag or a series of clicks is cut by captcha_geometry.split_gesture into requests whose
  worst case stays under the plugin ceiling, sent back to back with the button held (no read, no sleep in
  between); a 504 is `gesture_timeout`, another status `gesture_error`; after a failed gesture no further
  request of it, GESTURE_SETTLE_S, then the release;
- bounds: a step only starts with its own bound left before the deadline (`_need`), the paid SadCaptcha
  call only with the bound of everything needed to play its answer, so the solver hands back by its
  deadline (puzzle v2 keeps its own closed-loop bound, PUZZLE_V2_MAX_S);
- diagnostic: one entry per try in `CaptchaReport.diagnostics` (codes and numbers only, outside `to_json`),
  turned into lines by `diagnostic_lines` — printed by the jobs, never by this module.

s27 : lecture tenue et auto-calibration du rotate v2, console ADR 061 (amends ADR 059 §2 and §5 for rotate v2
only): the drag is played up to its final move with the button held (≤ 2 requests), the page is read ONCE with the
button still held (geometry_js, HELD_PROBE_TIMEOUT_S, any error ⇒ no read), captcha_geometry.rotate_target decides
at most one correction, and the final request (correction or not, then up) is always sent (try/finally). The
measures go at the end of the diagnostic line; a not_gone try whose read gave a negative scale makes the next try
aim at 360 − angle, and the one after back at the angle (alternation).

Hygiene: nothing is printed, no exception message is kept (the SadCaptcha key travels in its URL). The page is
never loaded again: the only control clicked between two tries is the captcha's own button (rotate v2).
"""

from __future__ import annotations

import base64
import math
import random
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from app import captcha_geometry as geo
from app import captcha_selectors as sel
from app.camofox_reader import CamofoxHTTPError, CamofoxUnreachable
from app.sadcaptcha import SadCaptchaClient, SadCaptchaError

MAX_TRIES = 3
MIN_TRY_BUDGET_S = 60.0
EVALUATE_TIMEOUT_S = 10.0
MOUSE_TIMEOUT_S = 25.0
FACTS_WINDOW_S = 30.0
VERIFY_WINDOW_S = 8.0
PAUSE_BETWEEN_TRIES_S = 2.0
PAUSE_AFTER_REFRESH_S = 3.0
REFRESH_LOOKUP_S = 5.0
GESTURE_SETTLE_S = 2.0
SADCAPTCHA_TIMEOUT_S = 15.0  # SadCaptchaClient.timeout
DRAG_MAX_REQUESTS = 3  # split_gesture(drag_actions(...)), tested in tests/test_captcha_geometry.py
HELD_PROBE_TIMEOUT_S = 2.5  # s27 (console ADR 061): the rotate v2 read with the button held
ROTATE_V2_MAX_REQUESTS = DRAG_MAX_REQUESTS  # ≤ 2 held requests + the final one (tests/test_captcha_geometry.py)
PUZZLE_V2_STEP_PX = 2
PUZZLE_V2_MAX_ITERATIONS = 200
PUZZLE_V2_MAX_S = 45.0
IMAGE_TIMEOUT_S = 10.0
ATTEMPT_MS_MAX = 600_000
REPORT_MAX_CAPTCHAS = 5
IMAGE_HOST_SUFFIXES = (".ibyteimg.com", ".byteimg.com", ".tiktokcdn.com", ".tiktokcdn-us.com", ".tiktokcdn-eu.com")
_B64_MAX_CHARS = (sel.IMAGE_MAX_BYTES + 2) // 3 * 4

CAPTCHA_OUTCOMES = ("solved", "unsolved", "unrecognized", "disabled", "unavailable", "service_error")

# Closed lists of the per-try diagnostic (console ADR 059, P5).
DIAGNOSTIC_STEPS = (
    "solved",
    "not_gone",
    "identify",
    "unrecognized",
    "images",
    "sadcaptcha",
    "rects",
    "gesture_timeout",
    "gesture_error",
    "budget",
    "unavailable",
)
CAPTCHA_BUTTON_STATES = ("clicked", "missing", "error")
# s27 (console ADR 061): closed lists of the rotate v2 measures.
PAIR_STATES = ("ok", "same")
ROTATE_SOURCES = geo.ROTATE_SOURCES
FIX_CODES = geo.FIX_CODES
SENSES = geo.SENSES
REST_STATES = geo.REST_STATES
MEASURE_FIELDS = (
    "bar", "handle", "track", "in_w", "in_nat", "out_w", "pair", "moved", "rot_in", "rot_out", "rsrc", "rsrc_out",
    "k1000", "target", "fix", "sense",
    "rot0_in", "rot0_out", "rot0",  # review m1: rotations read before the drag, appended last
)

# Injected by tests (P6 fallback); never given a cookie.
_urlopen = urllib.request.urlopen


@dataclass
class CaptchaReport:
    """Captchas met by one run (console ADR 052 format). `stop_run` is set by a SadCaptcha error."""

    captchas: list[dict[str, Any]] = field(default_factory=list)
    stop_run: bool = False
    # One entry per try (s25, console ADR 059): codes and numbers only, never in to_json.
    diagnostics: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> list[dict[str, Any]]:
        return [
            {
                "seq": c["seq"],
                "outcome": c["outcome"],
                "at": c["at"],
                "attempts": [dict(a) for a in c["attempts"][:MAX_TRIES]],
            }
            for c in self.captchas[:REPORT_MAX_CAPTCHAS]
        ]


class _Failed(Exception):
    """A try that did not solve the captcha, at a step of DIAGNOSTIC_STEPS (with the plugin status, if any)."""

    def __init__(self, step: str, http: int | None = None) -> None:
        super().__init__(step)
        self.step = step
        self.http = http


class _Stop(Exception):
    """End of the captcha with a given outcome (the try in progress is not logged)."""

    def __init__(self, outcome: str) -> None:
        super().__init__(outcome)
        self.outcome = outcome


class _Unrecognized(Exception):
    """No known variant on the page."""


def _image_host_allowed(src: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(src)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host.endswith(suffix) for suffix in IMAGE_HOST_SUFFIXES)


def _fetch_image(src: str) -> str | None:
    """P6 fallback when the in-page fetch fails (CORS): allowed CDN hosts only, no cookie, 2 MiB, 10 s."""
    if not _image_host_allowed(src):
        return None
    request = urllib.request.Request(src, headers={"Accept": "image/*"}, method="GET")
    try:
        with _urlopen(request, timeout=IMAGE_TIMEOUT_S) as response:
            data = response.read(sel.IMAGE_MAX_BYTES + 1)
    except (OSError, ValueError):
        return None
    except Exception:  # noqa: BLE001 - http.client errors: never kept
        return None
    if not data or len(data) > sel.IMAGE_MAX_BYTES:
        return None
    return base64.b64encode(data).decode("ascii")


def _new_diagnostic(seq: int, number: int) -> dict[str, Any]:
    return {
        "seq": seq,
        "try": number,
        "variant": "unknown",
        "step": "identify",
        "angle": None,
        "distance": None,
        "requests": 0,
        "moves": 0,
        "gesture_ms": 0,
        "http": None,
        "captcha_button": None,
        "ms": 0,
        **{name: None for name in MEASURE_FIELDS},
    }


class _Solver:
    def __init__(
        self,
        client: Any,
        tab_id: str,
        sadcaptcha: Any,
        *,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
        rng: random.Random,
        deadline: float,
    ) -> None:
        self.client = client
        self.tab = tab_id
        self.sad = sadcaptcha
        self.clock = clock
        self.sleep = sleep
        self.rng = rng
        self.deadline = deadline
        self.diag: dict[str, Any] = _new_diagnostic(0, 0)
        self.sense = "direct"  # s27 (P4): aim of the next rotate v2 try, alternated after a not_gone

    # --- bounds --------------------------------------------------------------------------------------------------

    def _need(self, seconds: float) -> None:
        """A step only starts with its own bound left before the deadline (console ADR 059, P4)."""
        if self.deadline - self.clock() < seconds:
            raise _Failed("budget")

    @staticmethod
    def _gesture_bound(requests: int) -> float:
        """Worst case of a split gesture: each request's timeout, the settle pause and the release."""
        return requests * MOUSE_TIMEOUT_S + GESTURE_SETTLE_S + MOUSE_TIMEOUT_S

    def _answer_bound(self, variant: str) -> float:
        """Everything needed to play a SadCaptcha answer: rectangles, the whole gesture, the check."""
        kind, _ = geo.variant_parts(variant)
        if variant == "rotate_v2":
            # s27 (console ADR 061): held requests + the final one, and the read with the button held in between.
            gesture = self._gesture_bound(ROTATE_V2_MAX_REQUESTS) + HELD_PROBE_TIMEOUT_S
        elif kind == "rotate" or variant == "puzzle_v1":
            gesture = self._gesture_bound(DRAG_MAX_REQUESTS)
        elif variant == "puzzle_v2":
            gesture = 0.0  # closed loop, bounded by PUZZLE_V2_MAX_S itself (unchanged)
        else:
            points = 2 if kind == "shapes" else geo.MAX_ICON_POINTS
            gesture = self._gesture_bound(math.ceil(points / 3)) + EVALUATE_TIMEOUT_S  # + the submit click
        return EVALUATE_TIMEOUT_S + gesture + VERIFY_WINDOW_S + EVALUATE_TIMEOUT_S

    # --- page reads ----------------------------------------------------------------------------------------------

    def evaluate(self, expression: str, step: str) -> Any:
        try:
            return self.client.evaluate_value(self.tab, expression, timeout=EVALUATE_TIMEOUT_S)
        except CamofoxUnreachable:
            raise _Stop("unavailable") from None
        except (CamofoxHTTPError, ValueError):
            raise _Failed(step) from None

    def identify(self) -> tuple[str, dict[str, Any]]:
        end = min(self.clock() + FACTS_WINDOW_S, self.deadline - EVALUATE_TIMEOUT_S)
        self._need(EVALUATE_TIMEOUT_S)
        read_any = False
        while True:
            try:
                facts = self.evaluate(sel.FACTS_JS, "identify")
            except _Failed:
                facts = None
            if isinstance(facts, dict):
                read_any = True
                variant = geo.identify(facts)
                if variant is not None:
                    return variant, facts
            if self.clock() + 1.0 > end:
                break
            self.sleep(1.0)
        if read_any:
            raise _Unrecognized()
        raise _Failed("identify")

    def image(self, selector: str) -> str:
        # In-page read, then the urllib fallback whose timeout applies to each operation (2 × IMAGE_TIMEOUT_S).
        self._need(EVALUATE_TIMEOUT_S + 2 * IMAGE_TIMEOUT_S)
        value = self.evaluate(sel.image_js(selector), "images")
        if not isinstance(value, dict):
            raise _Failed("images")
        b64 = value.get("b64")
        if isinstance(b64, str) and b64 and len(b64) <= _B64_MAX_CHARS:
            return b64
        src = value.get("src")
        fetched = _fetch_image(src) if isinstance(src, str) and src else None
        if fetched is None:
            raise _Failed("images")
        return fetched

    def rects(self, variant: str) -> dict[str, Any]:
        self._need(EVALUATE_TIMEOUT_S)
        value = self.evaluate(sel.geometry_js(variant), "rects")
        rects = value.get("rects") if isinstance(value, dict) else None
        if not isinstance(rects, dict):
            raise _Failed("rects")
        for name in sel.rect_names(variant):
            if not geo.valid_box(rects.get(name)):
                raise _Failed("rects")
        if variant == "rotate_v2":
            probe = value.get("probe") if isinstance(value.get("probe"), dict) else {}
            self.diag.update(
                bar=rects["bar"]["width"],
                handle=rects["handle"]["width"],
                track=_width(probe.get("track")),
                in_w=_width(probe.get("inner")),
                in_nat=probe.get("in_nat"),
                out_w=_width(probe.get("outer")),
                pair=probe.get("pair"),
                rot0_in=probe.get("rot_in"),
                rot0_out=probe.get("rot_out"),
            )
        return rects

    def held_probe(self) -> dict[str, Any] | None:
        """Rotate v2, button held (s27, console ADR 061): one bounded read; None on any error or without a handle.

        The client is called directly (not self.evaluate): an unreachable Camofox here is only a missing read, the
        final request that follows decides whether the captcha stops.
        """
        try:
            value = self.client.evaluate_value(self.tab, sel.geometry_js("rotate_v2"), timeout=HELD_PROBE_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - any failure of the read is `no_read`, never kept
            return None
        rects = value.get("rects") if isinstance(value, dict) else None
        if not isinstance(rects, dict) or not geo.valid_box(rects.get("handle")):
            return None
        probe = value.get("probe")
        return {"handle": rects["handle"], "probe": probe if isinstance(probe, dict) else {}}

    def translate_x(self, variant: str) -> int | None:
        value = self.evaluate(sel.geometry_js(variant), "gesture_error")
        translate = value.get("translate_x") if isinstance(value, dict) else None
        return translate if isinstance(translate, int) and not isinstance(translate, bool) else None

    def captcha_gone(self) -> bool:
        """Two consecutive probes 1 s apart without the captcha, within VERIFY_WINDOW_S."""
        self._need(VERIFY_WINDOW_S + EVALUATE_TIMEOUT_S)
        end = self.clock() + VERIFY_WINDOW_S
        absent = 0
        while self.clock() < end:
            self.sleep(1.0)
            if self.clock() > end:
                break
            try:
                present = self.evaluate(sel.PRESENT_JS, "not_gone")
            except _Failed:
                present = None
            absent = absent + 1 if present is False else 0
            if absent >= 2:
                return True
        return False

    # --- gestures ------------------------------------------------------------------------------------------------

    def mouse(self, actions: list[dict[str, Any]], *, hold: bool = False) -> None:
        try:
            self.client.mouse(self.tab, actions, hold=hold, timeout=MOUSE_TIMEOUT_S)
        except CamofoxUnreachable:
            raise _Stop("unavailable") from None
        except CamofoxHTTPError as error:
            self.diag["http"] = error.status
            raise _Failed("gesture_timeout" if error.status == 504 else "gesture_error", error.status) from None
        except ValueError:
            raise _Failed("gesture_error") from None
        self.diag["http"] = 200

    def release(self) -> None:
        try:
            self.client.mouse(self.tab, [{"type": "wait", "ms": 300}, {"type": "up"}], timeout=MOUSE_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - the plugin's own timer releases it anyway
            pass

    def play(self, actions: list[dict[str, Any]]) -> None:
        """One gesture, split into requests sent back to back (button held in between, console ADR 059, P1-P2)."""
        try:
            chunks = geo.split_gesture(actions)
        except ValueError:
            raise _Failed("gesture_error") from None
        self._need(self._gesture_bound(len(chunks)))
        began = self.clock()
        try:
            for chunk, hold in chunks:
                self.diag["requests"] += 1
                self.diag["moves"] += sum(1 for a in chunk if a["type"] == "move")
                self.mouse(chunk, hold=hold)
        except _Failed:
            self.diag["gesture_ms"] += int((self.clock() - began) * 1000)
            self.sleep(GESTURE_SETTLE_S)
            self.release()
            raise
        self.diag["gesture_ms"] += int((self.clock() - began) * 1000)

    def click(self, selector: str) -> None:
        try:
            self.client.click_selector(self.tab, selector, timeout=EVALUATE_TIMEOUT_S)
        except CamofoxUnreachable:
            raise _Stop("unavailable") from None
        except CamofoxHTTPError as error:
            raise _Failed("gesture_error", error.status) from None
        except ValueError:
            raise _Failed("gesture_error") from None

    def drag_until(self, variant: str, start: tuple[float, float], target: int) -> None:
        """Puzzle v2, closed loop: move 2 px at a time with the button held until the piece reaches the target."""
        sx, sy = start
        began = self.clock()
        try:
            self.mouse(
                [{"type": "move", "x": sx, "y": sy}, {"type": "wait", "ms": self.rng.randint(90, 900)}, {"type": "down"}],
                hold=True,
            )
            x = sx
            for _ in range(PUZZLE_V2_MAX_ITERATIONS):
                if self.clock() - began >= PUZZLE_V2_MAX_S:
                    break
                x += PUZZLE_V2_STEP_PX
                self.mouse([{"type": "move", "x": x, "y": sy}], hold=True)
                current = self.translate_x(variant)
                if current is not None and current >= target - 1:
                    break
        finally:
            self.release()

    def _send(self, actions: list[dict[str, Any]], hold: bool) -> None:
        self.diag["requests"] += 1
        self.diag["moves"] += sum(1 for a in actions if a["type"] == "move")
        self.mouse(actions, hold=hold)

    def rotate_v2(self, angle: Any, rects: dict[str, Any]) -> None:
        """Held drag to the estimate, one read with the button held, at most one correction, then the release."""
        sense = self.sense
        self.diag["sense"] = sense
        bar, handle = rects["bar"]["width"], rects["handle"]["width"]
        distance = geo.rotate_distance(bar, handle, geo.rotate_wanted(angle, sense))
        self.diag["distance"] = distance
        start = geo.drag_start(rects["handle"])
        try:
            chunks = geo.split_gesture(geo.drag_actions_held(start, distance, self.rng))
        except ValueError:
            raise _Failed("gesture_error") from None
        self._need(self._gesture_bound(len(chunks) + 1) + HELD_PROBE_TIMEOUT_S)
        began = self.clock()
        try:
            for chunk, hold in chunks:
                self._send(chunk, hold)
        except _Failed:
            self.diag["gesture_ms"] += int((self.clock() - began) * 1000)
            self.sleep(GESTURE_SETTLE_S)
            self.release()
            raise
        # No _need, no pause, no other read until the final request (console ADR 059 §5, ADR 061).
        final = geo.finish_actions(start, None)
        try:
            held = self.held_probe()
            moved = held["handle"]["x"] - rects["handle"]["x"] if held is not None else None
            probe = held["probe"] if held is not None else {}
            rot0_in, rot0_out = self.diag.get("rot0_in"), self.diag.get("rot0_out")
            self.diag.update(
                moved=moved,
                rot_in=probe.get("rot_in"),
                rot_out=probe.get("rot_out"),
                rsrc=probe.get("rsrc_in"),
                rsrc_out=probe.get("rsrc_out"),
                rot0=geo.rotate_rest_state(probe.get("rot_in"), probe.get("rot_out"), rot0_in, rot0_out),
            )
            fix = geo.rotate_target(
                angle, bar - handle, distance, moved, probe.get("rot_in"), probe.get("rot_out"),
                probe.get("rsrc_in"), probe.get("rsrc_out"), sense, rot0_in=rot0_in, rot0_out=rot0_out,
            )
            self.diag.update(
                fix=fix.code,
                target=fix.target,
                k1000=round(fix.k * 1000) if fix.k is not None and math.isfinite(fix.k) else None,
            )
            if fix.code == "moved":
                final = geo.finish_actions(start, fix.cursor)
        finally:
            try:
                self._send(final, False)
            except _Failed:
                self.sleep(GESTURE_SETTLE_S)
                self.release()
                raise
            finally:
                self.diag["gesture_ms"] += int((self.clock() - began) * 1000)

    def next_sense(self) -> None:
        """After a rotate v2 try ended not_gone (s27, P4): alternate the aim, only on a negative observed scale."""
        if self.diag.get("sense") == "complement":
            self.sense = "direct"
        elif (
            self.diag.get("fix") in ("none", "moved")
            and isinstance(self.diag.get("k1000"), int)
            and self.diag["k1000"] < 0
        ):
            self.sense = "complement"

    def gesture(self, variant: str, answer: Any, rects: dict[str, Any]) -> None:
        kind, _ = geo.variant_parts(variant)
        if variant == "rotate_v2":
            self.rotate_v2(answer, rects)
        elif kind == "rotate":
            distance = geo.rotate_distance(rects["bar"]["width"], rects["handle"]["width"], answer)
            self.diag["distance"] = distance
            self.play(geo.drag_actions(geo.drag_start(rects["handle"]), distance, self.rng))
        elif variant == "puzzle_v1":
            distance = geo.puzzle_distance(answer, rects["puzzle"]["width"])
            self.diag["distance"] = distance
            self.play(geo.drag_actions(geo.drag_start(rects["handle"]), distance, self.rng))
        elif variant == "puzzle_v2":
            target = geo.puzzle_distance(answer, rects["puzzle"]["width"])
            self.drag_until(variant, geo.drag_start(rects["handle"]), target)
        else:
            points = list(answer)
            box = rects["image"]
            self.play(geo.click_actions([geo.proportional_point(box, px, py) for px, py in points], self.rng))
            self._need(EVALUATE_TIMEOUT_S)
            self.click(sel.ELEMENTS[variant]["submit"])

    def captcha_button(self) -> str:
        """Between two rotate v2 tries: locate the captcha's own button again, click it by the plugin, pause."""
        end = self.clock() + REFRESH_LOOKUP_S
        rect = None
        while True:
            try:
                value = self.evaluate(sel.REFRESH_JS, "not_gone")
            except _Failed:
                value = None
            if geo.valid_box(value):
                rect = value
                break
            if self.clock() + 1.0 > end:
                break
            self.sleep(1.0)
        state = "missing"
        if rect is not None:
            centre = (rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2)
            try:
                self.client.mouse(self.tab, geo.click_actions([centre], self.rng), timeout=MOUSE_TIMEOUT_S)
                state = "clicked"
            except CamofoxUnreachable:
                raise _Stop("unavailable") from None
            except (CamofoxHTTPError, ValueError):
                state = "error"
        self.sleep(PAUSE_AFTER_REFRESH_S)
        return state

    # --- one try -------------------------------------------------------------------------------------------------

    def ask(self, variant: str, facts: dict[str, Any]) -> Any:
        kind, _ = geo.variant_parts(variant)
        images = [self.image(selector) for selector in sel.image_selectors(variant)]
        challenge = ""
        if kind == "icon":
            challenge = facts.get("challenge") if isinstance(facts.get("challenge"), str) else ""
            if not challenge.strip():
                raise _Failed("images")
        # The paid call only when its answer can be played to the end (console ADR 059, P4).
        self._need(SADCAPTCHA_TIMEOUT_S + self._answer_bound(variant))
        try:
            if kind == "rotate":
                answer = self.sad.rotate(images[0], images[1])
                self.diag["angle"] = int(answer)
                return answer
            if kind == "puzzle":
                return self.sad.puzzle(images[0], images[1])
            if kind == "shapes":
                return self.sad.shapes(images[0])
            return self.sad.icon(challenge, images[0])
        except SadCaptchaError:
            raise _Stop("service_error") from None

    def attempt(self, diag: dict[str, Any]) -> tuple[str, str]:
        """Returns (variant, result) with result solved | failed; raises _Unrecognized / _Stop."""
        self.diag = diag
        variant = "unknown_none"
        try:
            variant, facts = self.identify()
            diag["variant"] = variant
            answer = self.ask(variant, facts)
            rects = self.rects(variant)
            self.gesture(variant, answer, rects)
            if not self.captcha_gone():
                if variant == "rotate_v2":
                    self.next_sense()
                raise _Failed("not_gone")
            diag["step"] = "solved"
            return variant, "solved"
        except _Failed as failed:
            diag["step"] = failed.step
            return variant, "failed"


def _count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _width(box: Any) -> Any:
    return box.get("width") if isinstance(box, dict) else None


def _signed(value: Any) -> str:
    """A finite number rounded to an integer (sign kept), or `-` (s27)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "-"
    return str(round(value))


def _closed(value: Any, allowed: tuple[str, ...]) -> str:
    return value if isinstance(value, str) and value in allowed else "-"


def _measures(diag: dict[str, Any]) -> str:
    """s27 (console ADR 061): the rotate v2 measures, `-` everywhere for another variant."""
    if diag.get("variant") != "rotate_v2":
        return "".join(f" {name}=-" for name in MEASURE_FIELDS)
    closed = {
        "pair": PAIR_STATES, "rsrc": ROTATE_SOURCES, "rsrc_out": ROTATE_SOURCES, "fix": FIX_CODES, "sense": SENSES,
        "rot0": REST_STATES,
    }
    return "".join(
        f" {name}={_closed(diag.get(name), closed[name]) if name in closed else _signed(diag.get(name))}"
        for name in MEASURE_FIELDS
    )


def _code(value: Any) -> str:
    """An integer or `-`: never a free value on a diagnostic line."""
    if isinstance(value, bool) or not isinstance(value, int):
        return "-"
    return str(value)


def diagnostic_lines(report: CaptchaReport) -> list[str]:
    """One line per try (console ADR 059, P5): closed lists and numbers only, no text, URL, image nor key.

    The `refresh=` field is the fate of the captcha's own button after the try (rotate v2). An entry whose step
    is not in DIAGNOSTIC_STEPS is dropped. s27 (console ADR 061): the rotate v2 measures follow `ms=` (MEASURE_FIELDS,
    numbers rounded to integers and closed lists, `-` otherwise and for every other variant).
    """
    lines = []
    for diag in report.diagnostics:
        step = diag.get("step")
        if step not in DIAGNOSTIC_STEPS:
            continue
        variant = diag.get("variant") if diag.get("variant") in geo.VARIANTS else "unknown"
        http = diag.get("http")
        http_code = str(http) if isinstance(http, int) and not isinstance(http, bool) and 100 <= http <= 599 else "-"
        button = diag.get("captcha_button") if diag.get("captcha_button") in CAPTCHA_BUTTON_STATES else "-"
        lines.append(
            f"captcha {_count(diag.get('seq'))}.{_count(diag.get('try'))}: variant={variant} step={step}"
            f" angle={_code(diag.get('angle'))} distance={_code(diag.get('distance'))}"
            f" requests={_count(diag.get('requests'))} moves={_count(diag.get('moves'))}"
            f" gesture_ms={_count(diag.get('gesture_ms'))} http={http_code} refresh={button} ms={_count(diag.get('ms'))}"
            + _measures(diag)
        )
    return lines


def _attempt_entry(number: int, variant: str, result: str, ms: float) -> dict[str, Any]:
    if variant in geo.VARIANTS:
        kind, version = geo.variant_parts(variant)
    else:
        kind, version = "unknown", "none"
    return {"try": number, "type": kind, "variant": version, "result": result, "ms": max(0, min(ATTEMPT_MS_MAX, int(ms)))}


def solve_captcha(
    client: Any,
    tab_id: str,
    *,
    api_key: str,
    report: CaptchaReport,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    rng: random.Random,
    deadline: float,
    sadcaptcha: Any = None,
) -> str:
    """Solve the captcha shown in `tab_id`; append ONE captcha to `report` and return its outcome."""
    entry: dict[str, Any] = {"seq": len(report.captchas) + 1, "outcome": "", "at": int(clock() * 1000), "attempts": []}
    report.captchas.append(entry)

    def done(outcome: str) -> str:
        entry["outcome"] = outcome
        return outcome

    if report.stop_run:
        return done("service_error")
    key = (api_key or "").strip()
    if not key:
        return done("disabled")
    if not client.mouse_available():
        return done("unavailable")

    solver = _Solver(
        client,
        tab_id,
        sadcaptcha if sadcaptcha is not None else SadCaptchaClient(key),
        clock=clock,
        sleep=sleep,
        rng=rng,
        deadline=deadline,
    )
    attempts: list[dict[str, Any]] = entry["attempts"]
    for number in range(1, MAX_TRIES + 1):
        if deadline - clock() < MIN_TRY_BUDGET_S:
            return done("unsolved" if attempts else "unavailable")
        started = clock()
        diag = _new_diagnostic(entry["seq"], number)
        report.diagnostics.append(diag)
        try:
            variant, result = solver.attempt(diag)
        except _Unrecognized:
            diag["step"] = "unrecognized"
            diag["ms"] = int((clock() - started) * 1000)
            attempts.append(_attempt_entry(number, "", "unrecognized", (clock() - started) * 1000))
            return done("unrecognized")
        except _Stop as stop:
            diag["step"] = "sadcaptcha" if stop.outcome == "service_error" else "unavailable"
            diag["ms"] = int((clock() - started) * 1000)
            if stop.outcome == "service_error":
                report.stop_run = True
            return done(stop.outcome)
        diag["ms"] = int((clock() - started) * 1000)
        attempts.append(_attempt_entry(number, variant, result, (clock() - started) * 1000))
        if result == "solved":
            return done("solved")
        if diag["step"] == "budget":
            return done("unsolved")  # the budget only shrinks: a next try would stop at the same step
        if number < MAX_TRIES:
            if deadline - clock() < MIN_TRY_BUDGET_S:
                return done("unsolved")
            if variant == "rotate_v2":
                try:
                    diag["captcha_button"] = solver.captcha_button()
                except _Stop as stop:
                    diag["captcha_button"] = "error"
                    return done(stop.outcome)
            else:
                sleep(PAUSE_BETWEEN_TRIES_S)
    return done("unsolved")
