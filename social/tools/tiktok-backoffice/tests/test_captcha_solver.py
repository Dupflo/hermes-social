"""s22 — app/captcha_solver.py: bounded solver on a scripted fake Camofox client and a fake SadCaptcha.

Nothing reaches TikTok nor SadCaptcha: the only HTTP traffic is to 127.0.0.1 (image fallback, end-to-end case).
"""

from __future__ import annotations

import base64
import json
import random
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app import captcha_geometry as geo
from app import captcha_selectors as sel
from app import captcha_solver as solver
from app.camofox_reader import CamofoxHTTPError, CamofoxUnreachable
from app.captcha_solver import CaptchaReport, solve_captcha
from app.sadcaptcha import SadCaptchaClient, SadCaptchaError

KEY = "SADKEY-SENTINEL-123"
FACT_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "captcha" / "facts"
BOX_HANDLE = {"x": 100.0, "y": 400.0, "width": 40.0, "height": 40.0}
BOX_PUZZLE = {"x": 80.0, "y": 150.0, "width": 340.0, "height": 212.0}
BOX_BAR = {"x": 80.0, "y": 400.0, "width": 340.0, "height": 40.0}


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
    """Scripted Camofox client. `verdicts`: one list of PRESENT_JS answers per try (True = still there)."""

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
        if "mouse" in self.raise_on and not (len(actions) == 1 and actions[0]["type"] == "up"):
            raise self.raise_on["mouse"]
        if self.variant == "puzzle_v2" and hold and len(actions) == 1 and actions[0]["type"] == "move":
            self.translate_x += self.translate_step
        self.clock.t += self.mouse_step_ms / 1000
        return len(actions)

    def click_selector(self, tab_id, selector, *, timeout=None):
        self.calls.append(("click", selector))

    def _geometry(self):
        kind, _ = geo.variant_parts(self.variant)
        if kind == "puzzle":
            rects = {"puzzle": BOX_PUZZLE, "handle": BOX_HANDLE}
        elif kind == "rotate":
            rects = {"bar": BOX_BAR, "handle": BOX_HANDLE}
        else:
            rects = {"image": BOX_PUZZLE}
        out = {"rects": rects}
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
            if not self.current_verdicts:
                self.current_verdicts = list(self.verdicts.pop(0)) if self.verdicts else [True]
            value = self.current_verdicts.pop(0) if len(self.current_verdicts) > 1 else self.current_verdicts[0]
            return value
        if expression == sel.FACTS_JS:
            self.calls.append(("facts",))
            if "facts" in self.raise_on:
                raise self.raise_on["facts"]
            self.current_verdicts = []
            return self.facts
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
        assert len(mouses) == 1
        actions = mouses[0][2]
        assert mouses[0][3] is False
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
    refresh = [c for c in client.calls if c == ("click", sel.REFRESH_BUTTON)]
    assert len(refresh) == (2 if variant == "rotate_v2" else 0)


def test_unrecognized_after_five_seconds_of_facts():
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
    assert 2 <= len(facts_reads) <= 6
    assert clock() - start <= 6


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
    assert "refresh" not in source.replace("#captcha_refresh_button", "").replace("REFRESH_BUTTON", "")
