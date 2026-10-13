"""TikTok captcha solver: SadCaptcha + trusted gestures through the console-mouse plugin (s22, console ADR 052/053).

`solve_captcha` is public and knows nothing about the login: it takes a Camofox client, a tab, a key, a report
and a deadline, so the scan (s23) can call it as is. One call = one captcha = one entry in the report:

1. the run already met a SadCaptcha error  => `service_error`, no call at all;
2. no key (vault nor deploy/.env)           => `disabled`, no mouse nor SadCaptcha call;
3. the plugin probe fails                   => `unavailable`, no SadCaptcha call;
4. at most MAX_TRIES tries, each only with ≥ MIN_TRY_BUDGET_S left before the deadline. A try = facts →
   images → one SadCaptcha call → geometry read right before the gesture → gesture → check that the captcha
   is gone. Outcomes: solved | unsolved | unrecognized | service_error | unavailable.

Hygiene: nothing is printed, no exception message is kept (the SadCaptcha key travels in its URL). The page is
never loaded again: the only control clicked between two tries is the captcha's own button (rotate v2).
"""

from __future__ import annotations

import base64
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
FACTS_WINDOW_S = 5.0
VERIFY_WINDOW_S = 8.0
PAUSE_BETWEEN_TRIES_S = 2.0
PUZZLE_V2_STEP_PX = 2
PUZZLE_V2_MAX_ITERATIONS = 200
PUZZLE_V2_MAX_S = 45.0
IMAGE_TIMEOUT_S = 10.0
ATTEMPT_MS_MAX = 600_000
REPORT_MAX_CAPTCHAS = 5
IMAGE_HOST_SUFFIXES = (".ibyteimg.com", ".byteimg.com", ".tiktokcdn.com", ".tiktokcdn-us.com", ".tiktokcdn-eu.com")
_B64_MAX_CHARS = (sel.IMAGE_MAX_BYTES + 2) // 3 * 4

CAPTCHA_OUTCOMES = ("solved", "unsolved", "unrecognized", "disabled", "unavailable", "service_error")

# Injected by tests (P6 fallback); never given a cookie.
_urlopen = urllib.request.urlopen


@dataclass
class CaptchaReport:
    """Captchas met by one run (console ADR 052 format). `stop_run` is set by a SadCaptcha error."""

    captchas: list[dict[str, Any]] = field(default_factory=list)
    stop_run: bool = False

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
    """A try that did not solve the captcha."""


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

    # --- page reads ----------------------------------------------------------------------------------------------

    def evaluate(self, expression: str) -> Any:
        try:
            return self.client.evaluate_value(self.tab, expression, timeout=EVALUATE_TIMEOUT_S)
        except CamofoxUnreachable:
            raise _Stop("unavailable") from None
        except (CamofoxHTTPError, ValueError):
            raise _Failed() from None

    def identify(self) -> tuple[str, dict[str, Any]]:
        end = self.clock() + FACTS_WINDOW_S
        read_any = False
        while True:
            try:
                facts = self.evaluate(sel.FACTS_JS)
            except _Failed:
                facts = None
            if isinstance(facts, dict):
                read_any = True
                variant = geo.identify(facts)
                if variant is not None:
                    return variant, facts
            if self.clock() >= end:
                break
            self.sleep(1.0)
        if read_any:
            raise _Unrecognized()
        raise _Failed()

    def image(self, selector: str) -> str:
        value = self.evaluate(sel.image_js(selector))
        if not isinstance(value, dict):
            raise _Failed()
        b64 = value.get("b64")
        if isinstance(b64, str) and b64 and len(b64) <= _B64_MAX_CHARS:
            return b64
        src = value.get("src")
        fetched = _fetch_image(src) if isinstance(src, str) and src else None
        if fetched is None:
            raise _Failed()
        return fetched

    def rects(self, variant: str) -> dict[str, Any]:
        value = self.evaluate(sel.geometry_js(variant))
        rects = value.get("rects") if isinstance(value, dict) else None
        if not isinstance(rects, dict):
            raise _Failed()
        for name in sel.rect_names(variant):
            if not geo.valid_box(rects.get(name)):
                raise _Failed()
        return rects

    def translate_x(self, variant: str) -> int | None:
        value = self.evaluate(sel.geometry_js(variant))
        translate = value.get("translate_x") if isinstance(value, dict) else None
        return translate if isinstance(translate, int) and not isinstance(translate, bool) else None

    def captcha_gone(self) -> bool:
        """Two consecutive probes 1 s apart without the captcha, within VERIFY_WINDOW_S."""
        end = self.clock() + VERIFY_WINDOW_S
        absent = 0
        while self.clock() < end:
            self.sleep(1.0)
            try:
                present = self.evaluate(sel.PRESENT_JS)
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
        except (CamofoxHTTPError, ValueError):
            raise _Failed() from None

    def release(self) -> None:
        try:
            self.client.mouse(self.tab, [{"type": "wait", "ms": 300}, {"type": "up"}], timeout=MOUSE_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - the plugin's own timer releases it anyway
            pass

    def click(self, selector: str) -> None:
        try:
            self.client.click_selector(self.tab, selector, timeout=EVALUATE_TIMEOUT_S)
        except CamofoxUnreachable:
            raise _Stop("unavailable") from None
        except (CamofoxHTTPError, ValueError):
            raise _Failed() from None

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

    def gesture(self, variant: str, answer: Any, rects: dict[str, Any]) -> None:
        kind, _ = geo.variant_parts(variant)
        if kind == "rotate":
            distance = geo.rotate_distance(rects["bar"]["width"], rects["handle"]["width"], answer)
            self.mouse(geo.drag_actions(geo.drag_start(rects["handle"]), distance, self.rng))
        elif variant == "puzzle_v1":
            distance = geo.puzzle_distance(answer, rects["puzzle"]["width"])
            self.mouse(geo.drag_actions(geo.drag_start(rects["handle"]), distance, self.rng))
        elif variant == "puzzle_v2":
            target = geo.puzzle_distance(answer, rects["puzzle"]["width"])
            self.drag_until(variant, geo.drag_start(rects["handle"]), target)
        else:
            points = list(answer)
            box = rects["image"]
            self.mouse(geo.click_actions([geo.proportional_point(box, px, py) for px, py in points], self.rng))
            self.click(sel.ELEMENTS[variant]["submit"])

    # --- one try -------------------------------------------------------------------------------------------------

    def ask(self, variant: str, facts: dict[str, Any]) -> Any:
        kind, _ = geo.variant_parts(variant)
        images = [self.image(selector) for selector in sel.image_selectors(variant)]
        challenge = ""
        if kind == "icon":
            challenge = facts.get("challenge") if isinstance(facts.get("challenge"), str) else ""
            if not challenge.strip():
                raise _Failed()
        try:
            if kind == "rotate":
                return self.sad.rotate(images[0], images[1])
            if kind == "puzzle":
                return self.sad.puzzle(images[0], images[1])
            if kind == "shapes":
                return self.sad.shapes(images[0])
            return self.sad.icon(challenge, images[0])
        except SadCaptchaError:
            raise _Stop("service_error") from None

    def attempt(self) -> tuple[str, str]:
        """Returns (variant, result) with result solved | failed; raises _Unrecognized / _Stop."""
        variant = "unknown_none"
        try:
            variant, facts = self.identify()
            answer = self.ask(variant, facts)
            rects = self.rects(variant)
            self.gesture(variant, answer, rects)
            return variant, ("solved" if self.captcha_gone() else "failed")
        except _Failed:
            return variant, "failed"


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
        try:
            variant, result = solver.attempt()
        except _Unrecognized:
            attempts.append(_attempt_entry(number, "", "unrecognized", (clock() - started) * 1000))
            return done("unrecognized")
        except _Stop as stop:
            if stop.outcome == "service_error":
                report.stop_run = True
            return done(stop.outcome)
        attempts.append(_attempt_entry(number, variant, result, (clock() - started) * 1000))
        if result == "solved":
            return done("solved")
        if number < MAX_TRIES:
            if variant == "rotate_v2":
                try:
                    solver.click(sel.REFRESH_BUTTON)
                except _Failed:
                    pass
                except _Stop as stop:
                    return done(stop.outcome)
            else:
                sleep(PAUSE_BETWEEN_TRIES_S)
    return done("unsolved")
