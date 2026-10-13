"""s20: Camofox client (Bearer key, env config, typed errors) against a local HTTP server."""

from __future__ import annotations

import inspect
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from app.camofox_reader import (
    DEFAULT_CAMOFOX_BASE_URL,
    DEFAULT_CAMOFOX_USER_ID,
    CamofoxClient,
    CamofoxHTTPError,
    CamofoxUnreachable,
)


class _Recorder:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        # path (without query) -> (status, body dict) or, s21, (status, raw bytes, content type)
        self.responses: dict[tuple[str, str], tuple] = {}
        # s21: path (without query) -> seconds to wait before answering
        self.delays: dict[tuple[str, str], float] = {}


def _make_handler(recorder: _Recorder):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args, **kwargs):  # silence the test output
            return

        def _handle(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            parts = urlsplit(self.path)
            recorder.requests.append({
                "method": method,
                "path": parts.path,
                "query": parse_qs(parts.query),
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": json.loads(raw) if raw else None,
            })
            response = recorder.responses.get((method, parts.path), (200, {"ok": True}))
            delay = recorder.delays.get((method, parts.path))
            if delay:
                time.sleep(delay)
            if len(response) == 3:
                status, payload, content_type = response
            else:
                status, body = response
                payload, content_type = json.dumps(body).encode("utf-8"), "application/json"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_DELETE(self):
            self._handle("DELETE")

    return Handler


@pytest.fixture()
def server():
    recorder = _Recorder()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(recorder))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield recorder, f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_bearer_header_sent_only_when_key_set(server):
    recorder, base = server
    recorder.responses[("GET", "/health")] = (200, {"ok": True})
    CamofoxClient(base_url=base, api_key="k-123").health()
    CamofoxClient(base_url=base).health()
    assert recorder.requests[0]["headers"].get("authorization") == "Bearer k-123"
    assert "authorization" not in recorder.requests[1]["headers"]


def test_from_env_defaults_and_overrides():
    default = CamofoxClient.from_env({})
    assert default.base_url == DEFAULT_CAMOFOX_BASE_URL
    assert default.user_id == DEFAULT_CAMOFOX_USER_ID == "hermes_80317d7dba"
    assert default.api_key == ""
    custom = CamofoxClient.from_env({
        "CAMOFOX_URL": "http://other:1",
        "CAMOFOX_USER_ID": "hermes_x",
        "CAMOFOX_API_KEY": "  secret-key \n",
    })
    assert custom.base_url == "http://other:1"
    assert custom.user_id == "hermes_x"
    assert custom.api_key == "secret-key"


def test_repr_does_not_leak_key():
    client = CamofoxClient(api_key="SECRETAPIKEY")
    assert "SECRETAPIKEY" not in repr(client)
    assert "SECRETAPIKEY" not in str(client)


def test_list_tabs_sends_user_id_in_query(server):
    recorder, base = server
    recorder.responses[("GET", "/tabs")] = (
        200,
        {"running": True, "tabs": [{"tabId": "t1", "url": "https://www.tiktok.com/", "title": "x", "listItemId": "l"}]},
    )
    tabs = CamofoxClient(base_url=base, user_id="hermes_u").list_tabs()
    assert tabs == [{"tabId": "t1", "url": "https://www.tiktok.com/"}]
    assert recorder.requests[0]["query"] == {"userId": ["hermes_u"]}


def test_delete_tab_uses_query_user_id(server):
    recorder, base = server
    CamofoxClient(base_url=base, user_id="hermes_u").delete_tab("tab-9")
    req = recorder.requests[0]
    assert req["method"] == "DELETE"
    assert req["path"] == "/tabs/tab-9"
    assert req["query"] == {"userId": ["hermes_u"]}


def test_create_blank_tab_has_no_url(server):
    recorder, base = server
    recorder.responses[("POST", "/tabs")] = (200, {"tabId": "new-1"})
    tab_id = CamofoxClient(base_url=base, user_id="hermes_u").create_blank_tab("console-session-check")
    assert tab_id == "new-1"
    body = recorder.requests[0]["body"]
    assert body == {"userId": "hermes_u", "sessionKey": "console-session-check"}
    assert "url" not in body


def test_storage_state_path(server):
    recorder, base = server
    recorder.responses[("GET", "/sessions/hermes_u/storage_state")] = (200, {"cookies": [], "origins": []})
    state = CamofoxClient(base_url=base, user_id="hermes_u", api_key="k").storage_state()
    assert state == {"cookies": [], "origins": []}
    assert recorder.requests[0]["method"] == "GET"
    assert recorder.requests[0]["headers"]["authorization"] == "Bearer k"


def test_http_error_is_typed_without_body_or_key(server):
    recorder, base = server
    recorder.responses[("GET", "/sessions/hermes_u/storage_state")] = (
        403,
        {"error": "Forbidden BODYMARKER sessionid=SECRETCOOKIEVALUE"},
    )
    client = CamofoxClient(base_url=base, user_id="hermes_u", api_key="SECRETAPIKEY")
    with pytest.raises(CamofoxHTTPError) as info:
        client.storage_state()
    assert info.value.status == 403
    text = str(info.value) + repr(info.value)
    for leaked in ("BODYMARKER", "SECRETCOOKIEVALUE", "SECRETAPIKEY", base, "storage_state"):
        assert leaked not in text


def test_connection_refused_is_unreachable():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    with pytest.raises(CamofoxUnreachable):
        CamofoxClient(base_url=f"http://127.0.0.1:{port}").health()


def test_fetch_comments_reads_env(monkeypatch):
    from app import camofox_reader

    def fake_from_env(environ=None):
        return CamofoxClient(base_url="http://env:1", user_id="hermes_env", api_key="envkey")

    captured = []

    def spy_create(self, **kwargs):
        captured.append((self.base_url, self.user_id, self.api_key))
        raise CamofoxUnreachable()

    monkeypatch.setattr(camofox_reader.CamofoxClient, "from_env", staticmethod(fake_from_env))
    monkeypatch.setattr(camofox_reader.CamofoxClient, "create_tab", spy_create)
    result = camofox_reader.fetch_comments_from_camofox(video_url="https://www.tiktok.com/@a/video/1")
    assert result["error"] == "camofox_unreachable"
    camofox_reader.fetch_comments_from_camofox(
        video_url="https://www.tiktok.com/@a/video/1", base_url="http://explicit:2", user_id="hermes_explicit"
    )
    assert captured == [("http://env:1", "hermes_env", "envkey"), ("http://explicit:2", "hermes_explicit", "envkey")]


def test_cli_fetch_defaults_come_from_env(monkeypatch, tmp_path):
    from app import cli

    seen = {}

    def fake_fetch(**kwargs):
        seen.update(kwargs)
        return {"ok": False, "error": "x", "comments": []}

    monkeypatch.setattr(cli, "fetch_comments_from_camofox", fake_fetch)
    cli.run(["--db", str(tmp_path / "t.sqlite3"), "fetch-comments-camofox", "--video-url", "https://www.tiktok.com/@a/video/1"])
    assert seen["base_url"] is None and seen["user_id"] is None
    cli.run(["--db", str(tmp_path / "t.sqlite3"), "fetch-comments-camofox", "--video-url", "https://www.tiktok.com/@a/video/1", "--base-url", "http://b:1", "--user-id", "hermes_b"])
    assert seen["base_url"] == "http://b:1" and seen["user_id"] == "hermes_b"


# --- s21: login methods (navigate, keyboard typing, press, typed click/evaluate, binary screenshot) ---

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def test_type_text_uses_keyboard_mode_and_delay(server):
    recorder, base = server
    CamofoxClient(base_url=base, user_id="hermes_u").type_text("tab-1", "input[name=username]", "hello", delay_ms=70)
    req = recorder.requests[0]
    assert req["method"] == "POST"
    assert req["path"] == "/tabs/tab-1/type"
    assert req["body"] == {
        "userId": "hermes_u", "selector": "input[name=username]", "text": "hello", "mode": "keyboard", "delay": 70,
    }
    assert "submit" not in req["body"] and "pressEnter" not in req["body"]


def test_type_text_default_delay_is_60(server):
    recorder, base = server
    CamofoxClient(base_url=base).type_text("t", "#x", "a")
    assert recorder.requests[0]["body"]["delay"] == 60
    assert recorder.requests[0]["body"]["mode"] == "keyboard"


def test_type_text_409_is_typed_and_never_carries_the_text(server):
    recorder, base = server
    recorder.responses[("POST", "/tabs/t/type")] = (409, {"error": "page_changed BODYMARKER", "url": "https://x"})
    with pytest.raises(CamofoxHTTPError) as info:
        CamofoxClient(base_url=base).type_text("t", "input[type=password]", "SECRETPASSWORD")
    assert info.value.status == 409
    text = str(info.value) + repr(info.value)
    for leaked in ("SECRETPASSWORD", "BODYMARKER", base, "password"):
        assert leaked not in text


def test_navigate_body_and_default_timeout(server):
    recorder, base = server
    CamofoxClient(base_url=base, user_id="hermes_u").navigate("t-2", "https://www.tiktok.com/login")
    req = recorder.requests[0]
    assert (req["method"], req["path"]) == ("POST", "/tabs/t-2/navigate")
    assert req["body"] == {"userId": "hermes_u", "url": "https://www.tiktok.com/login"}
    assert inspect.signature(CamofoxClient.navigate).parameters["timeout"].default == 35


def test_navigate_slower_than_timeout_is_unreachable(server):
    recorder, base = server
    recorder.delays[("POST", "/tabs/t/navigate")] = 1.0
    with pytest.raises(CamofoxUnreachable):
        CamofoxClient(base_url=base).navigate("t", "https://www.tiktok.com/login", timeout=0.2)


def test_press_body(server):
    recorder, base = server
    CamofoxClient(base_url=base, user_id="hermes_u").press("t", "Enter")
    req = recorder.requests[0]
    assert (req["method"], req["path"]) == ("POST", "/tabs/t/press")
    assert req["body"] == {"userId": "hermes_u", "key": "Enter"}


def test_click_selector_and_evaluate_value_are_typed(server):
    recorder, base = server
    recorder.responses[("POST", "/tabs/t/evaluate")] = (200, {"ok": True, "result": {"form": True}})
    client = CamofoxClient(base_url=base, user_id="hermes_u")
    assert client.evaluate_value("t", "1+1") == {"form": True}
    client.click_selector("t", "button[type=submit]")
    assert recorder.requests[1]["body"] == {"userId": "hermes_u", "selector": "button[type=submit]"}
    recorder.responses[("POST", "/tabs/t/evaluate")] = (200, {"ok": False, "error": "BODYMARKER SECRETPASSWORD"})
    recorder.responses[("POST", "/tabs/t/click")] = (200, {"ok": False, "error": "BODYMARKER"})
    with pytest.raises(ValueError) as info:
        client.evaluate_value("t", "SECRETPASSWORD")
    assert "BODYMARKER" not in str(info.value) and "SECRETPASSWORD" not in str(info.value)
    with pytest.raises(ValueError) as info:
        client.click_selector("t", "#b")
    assert "BODYMARKER" not in str(info.value)
    recorder.responses[("POST", "/tabs/t/click")] = (422, {"error": "detached BODYMARKER"})
    with pytest.raises(CamofoxHTTPError) as http:
        client.click_selector("t", "#b")
    assert http.value.status == 422 and "BODYMARKER" not in str(http.value)


def test_screenshot_returns_png_bytes(server):
    recorder, base = server
    recorder.responses[("GET", "/tabs/t/screenshot")] = (200, PNG, "image/png")
    data = CamofoxClient(base_url=base, user_id="hermes_u", api_key="k").screenshot("t")
    assert data == PNG
    req = recorder.requests[0]
    assert req["method"] == "GET"
    assert req["query"] == {"userId": ["hermes_u"]}
    assert req["headers"]["accept"] == "image/png"
    assert req["headers"]["authorization"] == "Bearer k"


def test_screenshot_rejects_non_png(server):
    recorder, base = server
    recorder.responses[("GET", "/tabs/t/screenshot")] = (200, b"<html>BODYMARKER</html>", "text/html")
    with pytest.raises(ValueError) as info:
        CamofoxClient(base_url=base).screenshot("t")
    assert "BODYMARKER" not in str(info.value)


def test_screenshot_rejects_oversized_payload(server):
    recorder, base = server
    recorder.responses[("GET", "/tabs/t/screenshot")] = (200, PNG + b"\x00" * (8 * 1024 * 1024), "image/png")
    with pytest.raises(ValueError):
        CamofoxClient(base_url=base).screenshot("t")


def test_screenshot_http_error_is_typed(server):
    recorder, base = server
    recorder.responses[("GET", "/tabs/t/screenshot")] = (404, {"error": "BODYMARKER"})
    with pytest.raises(CamofoxHTTPError) as info:
        CamofoxClient(base_url=base).screenshot("t")
    assert info.value.status == 404 and "BODYMARKER" not in str(info.value)


# --- s22 (console ADR 053): console-mouse plugin routes.


def test_mouse_body_hold_default_and_bearer(server):
    recorder, base = server
    recorder.responses[("POST", "/tabs/tab-1/mouse")] = (200, {"ok": True, "performed": 2})
    client = CamofoxClient(base_url=base, user_id="hermes_x", api_key="k-123")
    actions = [{"type": "move", "x": 1, "y": 2}, {"type": "down"}]
    assert client.mouse("tab-1", actions) == 2
    assert client.mouse("tab-1", actions, hold=True) == 2
    first, second = recorder.requests
    assert first["path"] == "/tabs/tab-1/mouse"
    assert first["body"] == {"userId": "hermes_x", "actions": actions, "hold": False}
    assert second["body"] == {"userId": "hermes_x", "actions": actions, "hold": True}
    assert first["headers"].get("authorization") == "Bearer k-123"


def test_mouse_default_timeout_is_25():
    assert inspect.signature(CamofoxClient.mouse).parameters["timeout"].default == 25.0


def test_mouse_404_is_typed_without_body_or_key(server):
    recorder, base = server
    recorder.responses[("POST", "/tabs/tab-1/mouse")] = (404, {"error": "tab_not_found BODYSECRET"})
    with pytest.raises(CamofoxHTTPError) as info:
        CamofoxClient(base_url=base, api_key="KEYSECRET").mouse("tab-1", [{"type": "up"}])
    assert info.value.status == 404
    assert "BODYSECRET" not in str(info.value) and "KEYSECRET" not in str(info.value)
    assert "BODYSECRET" not in repr(info.value) and "KEYSECRET" not in repr(info.value)


@pytest.mark.parametrize("payload", [{"ok": False}, {"ok": True}, {"ok": True, "performed": "2"}, ["x"]])
def test_mouse_unexpected_payload_is_value_error(server, payload):
    recorder, base = server
    recorder.responses[("POST", "/tabs/tab-1/mouse")] = (200, payload)
    with pytest.raises(ValueError) as info:
        CamofoxClient(base_url=base).mouse("tab-1", [{"type": "up"}])
    assert str(info.value) == "unexpected mouse payload"


def test_mouse_available_true_on_plugin_health(server):
    recorder, base = server
    recorder.responses[("GET", "/console-mouse/health")] = (200, {"ok": True, "plugin": "console-mouse", "version": 1})
    assert CamofoxClient(base_url=base, api_key="k").mouse_available() is True
    assert recorder.requests[0]["headers"].get("authorization") == "Bearer k"


def test_mouse_available_default_timeout_is_5():
    assert inspect.signature(CamofoxClient.mouse_available).parameters["timeout"].default == 5.0


@pytest.mark.parametrize(
    "response",
    [
        (404, {"error": "not found"}),
        (403, {"error": "forbidden"}),
        (500, {"error": "x"}),
        (200, {"ok": True}),
        (200, {"ok": True, "plugin": "other"}),
        (200, {"ok": False, "plugin": "console-mouse"}),
        (200, b"not json", "text/plain"),
    ],
)
def test_mouse_available_false_otherwise(server, response):
    recorder, base = server
    recorder.responses[("GET", "/console-mouse/health")] = response
    assert CamofoxClient(base_url=base).mouse_available() is False


def test_mouse_available_false_when_server_is_down():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert CamofoxClient(base_url=f"http://127.0.0.1:{port}").mouse_available(timeout=1) is False
