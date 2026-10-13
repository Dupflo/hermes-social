"""s22 — app/sadcaptcha.py: SadCaptcha HTTP client against a local server (never the real service).

The licence key travels in the URL (upstream contract): every path is checked with a sentinel key.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from app.sadcaptcha import DEFAULT_BASE_URL, SadCaptchaClient, SadCaptchaError

KEY = "SADKEY-SENTINEL-123"


class _Recorder:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.response: tuple = (200, {})
        self.delay = 0.0
        self.cut = False


def _handler(recorder: _Recorder):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args, **kwargs):
            return

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            parts = urlsplit(self.path)
            recorder.requests.append({
                "path": parts.path,
                "query": parse_qs(parts.query),
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": json.loads(raw) if raw else None,
            })
            if recorder.cut:
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            if recorder.delay:
                time.sleep(recorder.delay)
            status, body = recorder.response
            payload = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


@pytest.fixture()
def server():
    recorder = _Recorder()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(recorder))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield recorder, f"http://127.0.0.1:{httpd.server_address[1]}/api/v1"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_default_base_url_is_the_public_api_and_only_the_constructor_overrides_it(monkeypatch):
    assert DEFAULT_BASE_URL == "https://www.sadcaptcha.com/api/v1"
    monkeypatch.setenv("SADCAPTCHA_BASE_URL", "http://evil.example")
    assert SadCaptchaClient(KEY).base_url == DEFAULT_BASE_URL
    assert SadCaptchaClient(KEY).timeout == 15.0


CALLS = [
    ("rotate", lambda c: c.rotate("OUT", "IN"), {"outerImageB64": "OUT", "innerImageB64": "IN"}, {"angle": 90}, 90),
    ("puzzle", lambda c: c.puzzle("PZ", "PC"), {"puzzleImageB64": "PZ", "pieceImageB64": "PC"},
     {"slideXProportion": 0.25}, 0.25),
    ("shapes", lambda c: c.shapes("IMG"), {"imageB64": "IMG"},
     {"pointOneProportionX": 0.1, "pointOneProportionY": 0.2, "pointTwoProportionX": 0.3, "pointTwoProportionY": 0.4},
     ((0.1, 0.2), (0.3, 0.4))),
    ("icon", lambda c: c.icon("Pick the hat", "IMG"), {"challenge": "Pick the hat", "imageB64": "IMG"},
     {"proportionalPoints": [{"proportionX": 0.5, "proportionY": 0.5}]}, [(0.5, 0.5)]),
]


@pytest.mark.parametrize(("kind", "invoke", "body", "answer", "expected"), CALLS, ids=[c[0] for c in CALLS])
def test_each_type_posts_its_body_and_returns_typed_values(server, kind, invoke, body, answer, expected):
    recorder, base = server
    recorder.response = (200, answer)
    client = SadCaptchaClient(KEY, base_url=base)
    assert invoke(client) == expected
    request = recorder.requests[0]
    assert request["path"] == f"/api/v1/{kind}"
    assert request["query"] == {"licenseKey": [KEY]}
    assert request["body"] == body
    assert request["headers"]["content-type"] == "application/json"


def _assert_clean(exc: BaseException, capsys, client: SadCaptchaClient) -> None:
    out = capsys.readouterr()
    for text in (str(exc), repr(exc), repr(client), str(client), out.out, out.err):
        assert KEY not in text
        assert "licenseKey" not in text
    assert exc.__cause__ is None
    assert exc.__context__ is None


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        ((400, {"error": f"bad key {KEY}"}), "http"),
        ((402, {"error": "no credits"}), "http"),
        ((500, b"oops"), "http"),
        ((200, b"{not json"), "invalid"),
        ((200, {"angle": "x"}), "invalid"),
        ((200, {}), "invalid"),
        ((200, ["angle"]), "invalid"),
    ],
)
def test_errors_are_typed_and_never_leak_the_key(server, capsys, response, kind):
    recorder, base = server
    recorder.response = response
    client = SadCaptchaClient(KEY, base_url=base)
    with pytest.raises(SadCaptchaError) as info:
        client.rotate("OUT", "IN")
    assert info.value.kind == kind
    assert str(info.value) == f"sadcaptcha {kind}"
    _assert_clean(info.value, capsys, client)
    assert len(recorder.requests) == 1  # no retry


def test_timeout_is_typed(server, capsys):
    recorder, base = server
    recorder.response = (200, {"angle": 1})
    recorder.delay = 1.0
    client = SadCaptchaClient(KEY, base_url=base, timeout=0.2)
    with pytest.raises(SadCaptchaError) as info:
        client.rotate("OUT", "IN")
    assert info.value.kind == "timeout"
    _assert_clean(info.value, capsys, client)


def test_connection_cut_is_typed(server, capsys):
    recorder, base = server
    recorder.cut = True
    client = SadCaptchaClient(KEY, base_url=base)
    with pytest.raises(SadCaptchaError) as info:
        client.puzzle("PZ", "PC")
    assert info.value.kind in {"http", "timeout"}
    _assert_clean(info.value, capsys, client)


def test_connection_refused_is_typed(capsys):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    client = SadCaptchaClient(KEY, base_url=f"http://127.0.0.1:{port}/api/v1", timeout=1)
    with pytest.raises(SadCaptchaError) as info:
        client.icon("c", "IMG")
    assert info.value.kind == "http"
    _assert_clean(info.value, capsys, client)


def test_success_path_prints_nothing(server, capsys):
    recorder, base = server
    recorder.response = (200, {"angle": 12})
    client = SadCaptchaClient(KEY, base_url=base)
    client.rotate("OUT", "IN")
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""
    assert KEY not in repr(client)
